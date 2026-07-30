//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Text.Json;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests.Server;

public sealed partial class WebhookEgressGuardTests
{
    // ── `localhost` by name, not just by address ────────────────────────────

    [Fact]
    public void ClassifyingLocalhostByNameWithoutTheKeyIsRefusedAsLoopback()
    {
        // §2/§3 through the name branch rather than the address branch: the two
        // are separate code paths (RFC 6761 name match vs. resolved-address
        // classification), and only a direct assertion on `Loopback` — not just
        // "refused" — pins that this path reports it correctly, the same way the
        // §4 tunnel check needs to know.
        //
        // A resolver that answers a *public* address is the point: if the name
        // check ever loosened (e.g. `host == "localhost" || …` weakened to
        // `&&`, which is never true since "localhost" cannot also end with the
        // 10-character ".localhost"), execution falls through to DNS — and the
        // realOS resolver would answer "localhost" from the hosts file with
        // 127.0.0.1 anyway, silently converging on the same verdict. Wiring in
        // a resolver that answers wrong is what makes a fallthrough visible.
        var resolver = ScriptedResolver.Answering("93.184.216.34");
        var policy = new WebhookEgressPolicy(allowLoopbackDestinations: false, resolver: resolver);

        var decision = policy.Classify("http://localhost/hook");

        Assert.False(decision.Allowed);
        Assert.True(decision.Loopback);
        Assert.Contains("loopback", decision.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void ClassifyingLocalhostByNameWithTheKeySetIsAccepted()
    {
        // The other half: with the key set, `localhost` is accepted outright —
        // reported as loopback (§4 still needs to know), with no refusal reason.
        // Same wrong-answer resolver as the row above: `Allowed` alone would
        // stay true either way (loopback-by-name or public-by-DNS both pass
        // when the key is set), so it is `Loopback` that has to catch a
        // fallthrough to DNS.
        var resolver = ScriptedResolver.Answering("93.184.216.34");
        var policy = new WebhookEgressPolicy(allowLoopbackDestinations: true, resolver: resolver);

        var decision = policy.Classify("http://localhost/hook");

        Assert.True(decision.Allowed);
        Assert.True(decision.Loopback);
        Assert.Equal("", decision.Reason);
    }

    private sealed class HangingResolver : IHostResolver
    {
        public Task<IReadOnlyList<IPAddress>> ResolveAsync(
            string host,
            CancellationToken cancellationToken = default) =>
            new TaskCompletionSource<IReadOnlyList<IPAddress>>().Task;
    }

    // ── §4 delivery-time tunnel check ───────────────────────────────────────

    /// <summary>
    /// Create a tunnel the way an operator does, and return its id.
    /// </summary>
    /// <remarks>
    /// In this port a tunnel is itself a session: the route mints
    /// <c>tunnel-&lt;id&gt;</c>, registers a session definition under that id and
    /// stores the share/control token hashes against it. So "this session has a
    /// live tunnel share" is asked of the tunnel store under the session's own
    /// id.
    /// </remarks>
    private static async Task<(string TunnelId, double ExpiresAt)> CreateTunnelAsync(HttpClient client)
    {
        var response = await client.PostAsync(
            "/api/tunnels",
            new StringContent(
                """{"tunnel_type":"terminal","display_name":"shared"}""",
                System.Text.Encoding.UTF8,
                "application/json"));
        response.EnsureSuccessStatusCode();
        var body = JsonDocument.Parse(await response.Content.ReadAsStringAsync()).RootElement;
        return (body.GetProperty("tunnel_id").GetString()!, body.GetProperty("expires_at").GetDouble());
    }

    private static async Task<WebhookConfig> RegisterOnSessionAsync(
        UtermServer server,
        HttpClient client,
        string sessionId,
        string url)
    {
        var response = await client.PostAsync(
            $"/api/sessions/{sessionId}/webhooks",
            new StringContent(
                $"{{\"url\":\"{url}\"}}",
                System.Text.Encoding.UTF8,
                "application/json"));
        await AssertAcceptedAsync(response);
        var id = JsonDocument.Parse(await response.Content.ReadAsStringAsync())
            .RootElement.GetProperty("webhook_id").GetString()!;
        return server.WebhooksForTests.GetWebhook(id)!;
    }

    private static async Task<long> MetricAsync(HttpClient client, string name)
    {
        var response = await client.GetAsync("/api/metrics");
        response.EnsureSuccessStatusCode();
        var metrics = JsonDocument.Parse(await response.Content.ReadAsStringAsync())
            .RootElement.GetProperty("metrics");
        return metrics.TryGetProperty(name, out var value) ? value.GetInt64() : 0;
    }

    [Fact]
    public async Task ALoopbackDeliveryIsRefusedWhileTheSessionIsTunnelShared()
    {
        // Loopback bind, so §3 permits loopback destinations and registration
        // succeeds — this row is about what changes afterwards. Sharing the
        // session through a relay means "bound to loopback" no longer implies
        // "only local callers exist", so the justification for allowing a
        // loopback destination is gone while the share is live.
        var (server, client) = await BootAsync();
        await using (server)
        {
            using (client)
            {
                var (tunnelId, _) = await CreateTunnelAsync(client);
                var cfg = await RegisterOnSessionAsync(server, client, tunnelId, "http://127.0.0.1:9/hook");

                var allowed = server.WebhooksForTests.IsDeliveryAllowed(cfg);

                Assert.False(allowed);
                // An operator has to be able to see this happening: the refusal
                // is invisible in the config, since the share that caused it was
                // issued at runtime.
                Assert.Equal(1, await MetricAsync(client, WebhookManager.DeliveryBlockedTunnelMetric));
            }
        }
    }

    [Fact]
    public async Task ATunnelRefusalDoesNotAdvanceTheGenericBlockedCounter()
    {
        // §4, the counter clause. The generic counter is what feeds the
        // three-strike auto-unregister, and a tunnel share is revocable at any
        // moment — so counting a share refusal there lets a few minutes of
        // sharing permanently delete a webhook that was never misconfigured.
        // The dedicated counter exists precisely so the two are told apart:
        // "suppressed while you are sharing" is not "this destination has gone
        // bad".
        var (server, client) = await BootAsync();
        await using (server)
        {
            using (client)
            {
                var (tunnelId, _) = await CreateTunnelAsync(client);
                var cfg = await RegisterOnSessionAsync(server, client, tunnelId, "http://127.0.0.1:9/hook");

                // More refusals than the auto-unregister threshold, so a run
                // that fed these to the generic counter would already have
                // retired the webhook by now.
                Assert.False(server.WebhooksForTests.IsDeliveryAllowed(cfg));
                Assert.False(server.WebhooksForTests.IsDeliveryAllowed(cfg));
                Assert.False(server.WebhooksForTests.IsDeliveryAllowed(cfg));
                Assert.False(server.WebhooksForTests.IsDeliveryAllowed(cfg));

                Assert.Equal(4, await MetricAsync(client, WebhookManager.DeliveryBlockedTunnelMetric));
                Assert.Equal(0, await MetricAsync(client, WebhookManager.DeliveryBlockedMetric));
                // And the webhook is still registered: the share suppressed
                // deliveries, it did not retire the destination.
                Assert.NotNull(server.WebhooksForTests.GetWebhook(cfg.WebhookId));
            }
        }
    }

    [Fact]
    public async Task ALoopbackDeliveryProceedsWhenTheSessionWasNeverShared()
    {
        // The other half of the row above: without a share, a loopback-bound
        // server delivering to loopback is the ordinary single-box case, and a
        // guard that refuses it anyway is stuck closed.
        var (server, client) = await BootAsync();
        await using (server)
        {
            using (client)
            {
                var cfg = await RegisterOnSessionAsync(server, client, "demo", "http://127.0.0.1:9/hook");

                Assert.True(server.WebhooksForTests.IsDeliveryAllowed(cfg));
                Assert.Equal(0, await MetricAsync(client, WebhookManager.DeliveryBlockedTunnelMetric));
            }
        }
    }

    [Fact]
    public async Task ALoopbackDeliveryProceedsOnceTheShareHasExpired()
    {
        // The check asks whether a share is live *now*. A share that has aged
        // out cannot expose anything, so it must not keep the guard closed —
        // otherwise one expired tunnel silently disables a session's webhooks
        // for the rest of the process's life.
        var clock = new ManualClock(1_700_000_000);
        var (server, client) = await BootAsync(clock: clock);
        await using (server)
        {
            using (client)
            {
                var (tunnelId, expiresAt) = await CreateTunnelAsync(client);
                var cfg = await RegisterOnSessionAsync(server, client, tunnelId, "http://127.0.0.1:9/hook");
                Assert.False(server.WebhooksForTests.IsDeliveryAllowed(cfg));

                clock.SetWall(expiresAt + 1);

                Assert.True(server.WebhooksForTests.IsDeliveryAllowed(cfg));
            }
        }
    }

    [Fact]
    public async Task TheShareIsNotLiveAtTheExactInstantItExpires()
    {
        // §4 pins the boundary: `expires_at == now` is NOT live. That is
        // deliberately the opposite of the `now > expires_at` convention this
        // port's invite consumption uses, and the contract fixes it so all four
        // ports agree on the instant — the reference spells it `now <
        // expires_at`, Go spells it `rec.ExpiresAt > now`, and this port had
        // followed its local invite convention instead (`now <= ExpiresAt`),
        // which makes the share live for one instant longer than everywhere else.
        //
        // The instant is unobservable against a real clock; parity is the point,
        // and a fixed clock can sit exactly on it.
        var clock = new ManualClock(1_700_000_000);
        var (server, client) = await BootAsync(clock: clock);
        await using (server)
        {
            using (client)
            {
                var (tunnelId, expiresAt) = await CreateTunnelAsync(client);
                var cfg = await RegisterOnSessionAsync(server, client, tunnelId, "http://127.0.0.1:9/hook");
                Assert.False(server.WebhooksForTests.IsDeliveryAllowed(cfg));

                clock.SetWall(expiresAt);

                Assert.True(server.WebhooksForTests.IsDeliveryAllowed(cfg));
            }
        }
    }

    [Fact]
    public async Task ALoopbackDeliveryProceedsOnceTheShareIsRevoked()
    {
        // Revocation deletes the token record, which is the same question asked
        // of the same state — pinned because "expired" and "revoked" are
        // different code paths in the tunnel store.
        var (server, client) = await BootAsync();
        await using (server)
        {
            using (client)
            {
                var (tunnelId, _) = await CreateTunnelAsync(client);
                var cfg = await RegisterOnSessionAsync(server, client, tunnelId, "http://127.0.0.1:9/hook");
                Assert.False(server.WebhooksForTests.IsDeliveryAllowed(cfg));

                (await client.DeleteAsync($"/api/tunnels/{tunnelId}/tokens")).EnsureSuccessStatusCode();

                Assert.True(server.WebhooksForTests.IsDeliveryAllowed(cfg));
            }
        }
    }

    [Fact]
    public async Task APublicDeliveryProceedsEvenWhileTheSessionIsTunnelShared()
    {
        // The tunnel check is about loopback destinations only. A shared session
        // is not a reason to stop delivering to a public endpoint, and a guard
        // that widened it to everything would break sharing for no security
        // gain.
        var (server, client) = await BootAsync();
        await using (server)
        {
            using (client)
            {
                var (tunnelId, _) = await CreateTunnelAsync(client);
                var cfg = await RegisterOnSessionAsync(server, client, tunnelId, "https://93.184.216.34/hook");

                Assert.True(server.WebhooksForTests.IsDeliveryAllowed(cfg));
            }
        }
    }

    [Fact]
    public async Task ADeliveryIsRefusedWhenTheDestinationStartsResolvingIntoBlockedSpace()
    {
        // Registration is a moment; delivery is forever. A name that answered
        // with a public address at registration can answer with the metadata IP
        // later, so the destination is re-classified at delivery rather than
        // trusted because it passed once.
        var answers = new Queue<string>(new[] { "93.184.216.34", "169.254.169.254" });
        var resolver = new ScriptedResolver(_ => new[] { IPAddress.Parse(answers.Dequeue()) });
        var (server, client) = await BootAsync(resolver: resolver);
        await using (server)
        {
            using (client)
            {
                var cfg = await RegisterOnSessionAsync(server, client, "demo", "http://hook.example.test/h");

                Assert.False(server.WebhooksForTests.IsDeliveryAllowed(cfg));
                Assert.Equal(1, await MetricAsync(client, WebhookManager.DeliveryBlockedMetric));
            }
        }
    }

    // ── §6 last row: the guard is not stuck closed ──────────────────────────

    [Fact]
    public async Task APublicDestinationIsAccepted()
    {
        // 93.184.216.34 is example.com's documented address: public, routable,
        // and in none of the blocked ranges. Written as a literal so the row
        // does not depend on a resolver.
        var (server, client) = await BootAsync();
        await using (server)
        {
            using (client)
            {
                await AssertAcceptedAsync(await RegisterAsync(client, "https://93.184.216.34/hook"));
            }
        }
    }

    [Theory]
    [InlineData("http://100.63.255.255:8080/hook")]
    [InlineData("http://100.128.0.0:8080/hook")]
    public async Task TheAddressesEitherSideOfCgnatAreStillAccepted(string url)
    {
        // Pins the /10 rather than the addresses inside it. Widened to a /8, the
        // CGNAT entry would swallow all of 100.0.0.0/8 and every refusal test
        // above would still pass — these two sit immediately either side of the
        // boundary, so only the correct mask satisfies both them and the
        // refusals.
        var (server, client) = await BootAsync();
        await using (server)
        {
            using (client)
            {
                await AssertAcceptedAsync(await RegisterAsync(client, url));
            }
        }
    }

    // ── the library-level posture ───────────────────────────────────────────

    [Fact]
    public void AnEmbedderWhoConfiguresNothingGetsTheClosedPosture()
    {
        // Constructed the way an embedder would, with none of the seams filled
        // in. The interesting part is what the defaults are: loopback refused
        // (matching the reference's `allow_loopback_destinations: bool = False`),
        // no session considered tunnel-shared, and no metric sink to invoke —
        // "no counter configured" must not become "no guard".
        var manager = new WebhookManager();

        Assert.Throws<ArgumentException>(() => manager.ValidateUrl("http://127.0.0.1:9/hook"));

        var loopbackAllowed = new WebhookManager(allowLoopbackDestinations: true);
        var cfg = loopbackAllowed.Register("s1", "http://127.0.0.1:9/hook", null, null, null);
        Assert.True(loopbackAllowed.IsDeliveryAllowed(cfg));

        var blocked = new WebhookConfig { SessionId = "s1", Url = "http://169.254.169.254/latest/" };
        Assert.False(loopbackAllowed.IsDeliveryAllowed(blocked));
    }

    [Fact]
    public async Task TheProductionResolverAnswersForALocalName()
    {
        // The default seam, exercised once so it is not shipped untried. Only
        // `localhost` — answered from the hosts file, so this needs no network
        // and reaches nothing.
        var addresses = await new DnsHostResolver().ResolveAsync("localhost");

        Assert.NotEmpty(addresses);
        Assert.All(addresses, ip => Assert.True(IPAddress.IsLoopback(ip), ip.ToString()));
    }

    [Fact]
    public async Task APublicHostnameIsAccepted()
    {
        // And the same through a resolver, which is how a real destination
        // arrives: the DNS path has to be able to say yes.
        var (server, client) = await BootAsync(resolver: ScriptedResolver.Answering("93.184.216.34"));
        await using (server)
        {
            using (client)
            {
                await AssertAcceptedAsync(await RegisterAsync(client, "https://hook.example.test/h"));
            }
        }
    }
}
