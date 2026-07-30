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

/// <summary>
/// The egress-guard matrix from <c>conformance/EGRESS_GUARD.md</c> §6, for C#.
///
/// A webhook destination is a URL the *server* fetches on behalf of whoever can
/// mutate a session, so it is attacker-influenced input driving a request from a
/// more privileged network position than the caller holds — SSRF. Before this
/// suite the whole C# guard was one <c>uri.IsLoopback</c> test, which means
/// <c>169.254.169.254</c> (cloud credentials) and <c>10.x</c> admin ports were
/// reachable no matter what an operator configured.
///
/// Every row is driven the way an operator reaches it — TOML → the production
/// factory (<see cref="ServerFactory.CreateFromConfig"/>) → the real
/// <c>POST /api/sessions/{id}/webhooks</c> route — rather than by calling the
/// validator directly, because the bug was never in the validator: it was in
/// what the factory handed it.
///
/// The last row (a public destination is accepted) carries as much weight as the
/// refusals: a guard that refuses everything passes every negative test, and
/// only that row distinguishes "wired" from "stuck closed".
/// </summary>
public sealed class WebhookEgressGuardTests
{
    private static int FreePort()
    {
        var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        listener.Start();
        var port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    /// <summary>
    /// Boot through the production factory from an actual TOML file.
    /// </summary>
    /// <param name="bindHost">
    /// What <c>[server] host</c> says, which per §3 is half of the effective
    /// loopback permission. The listener itself is always loopback — the URL
    /// handed to <see cref="UtermServer.Build"/> is separate from
    /// <c>cfg.Server.Host</c> — so a routable-bind row exercises the routable
    /// branch without this suite ever opening a public port.
    /// </param>
    private static async Task<(UtermServer Server, HttpClient Client)> BootAsync(
        string toml = "",
        string bindHost = "127.0.0.1",
        IHostResolver? resolver = null,
        IClock? clock = null)
    {
        var port = FreePort();
        var path = Path.Combine(Path.GetTempPath(), "uterm-egress-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(path, toml);
        UtermServerConfig cfg;
        try
        {
            cfg = ConfigLoader.Load(path);
        }
        finally
        {
            File.Delete(path);
        }

        cfg.Server.Host = bindHost;
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "demo",
            DisplayName = "Demo",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "admin",
        });

        var (server, _) = ServerFactory.CreateFromConfig(
            cfg, "egress-matrix", clock: clock, hostResolver: resolver);
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "eg-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = new[] { "admin" },
        });

        var client = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}") };
        client.DefaultRequestHeaders.Authorization =
            new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", token);
        return (server, client);
    }

    /// <summary>Register a webhook aimed at <paramref name="url"/>.</summary>
    /// <remarks>
    /// A measured <see cref="StringContent"/> rather than <c>PostAsJsonAsync</c>:
    /// the latter streams without a Content-Length and <c>ReadJson</c>
    /// (UtermServer.cs:1365) reads a null length as an empty body, so the request
    /// would arrive as <c>{}</c> and every assertion here would really be about
    /// "url is required".
    /// </remarks>
    private static Task<HttpResponseMessage> RegisterAsync(HttpClient client, string url) =>
        client.PostAsync(
            "/api/sessions/demo/webhooks",
            new StringContent(
                $"{{\"url\":\"{url}\",\"event_types\":[\"session.started\"]}}",
                System.Text.Encoding.UTF8,
                "application/json"));

    /// <summary>Assert the route refused, and that it refused for this reason.</summary>
    private static async Task AssertRefusedAsync(HttpResponseMessage response, string expectedReasonFragment)
    {
        var body = await response.Content.ReadAsStringAsync();
        // Asserting on the guard's own message, not merely on "not 200": any
        // refusal reason satisfies "not OK" — a malformed payload, a 404, an
        // authz failure — and only one of them is the guard doing its job.
        Assert.Equal(HttpStatusCode.UnprocessableEntity, response.StatusCode);
        Assert.Contains(expectedReasonFragment, body, StringComparison.OrdinalIgnoreCase);
    }

    private static async Task AssertAcceptedAsync(HttpResponseMessage response)
    {
        var body = await response.Content.ReadAsStringAsync();
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Contains("webhook_id", body, StringComparison.Ordinal);
    }

    // ── §3 the effective permission ─────────────────────────────────────────

    [Fact]
    public async Task LoopbackBindWithNoKeyAcceptsALoopbackDestination()
    {
        // The default bind is 127.0.0.1. Refusing loopback destinations there
        // protects nothing — no remote caller can reach the listener at all —
        // while breaking every single-box deployment, so the bind address is
        // half of the effective permission (§3).
        var (server, client) = await BootAsync();
        await using (server)
        {
            using (client)
            {
                await AssertAcceptedAsync(await RegisterAsync(client, "http://127.0.0.1:9/hook"));
            }
        }
    }

    [Theory]
    [InlineData("http://127.0.0.1:9/hook")]
    [InlineData("http://127.9.9.9:9/hook")]
    [InlineData("http://[::1]:9/hook")]
    public async Task RoutableBindWithNoKeyRefusesALoopbackDestination(string url)
    {
        // Once the listener is reachable from the network, "bound to loopback"
        // stops being an access-control statement about the *caller*, and a
        // loopback destination converts services that are unreachable by design
        // into reachable ones.
        //
        // All of 127.0.0.0/8 counts, not just 127.0.0.1 — a service bound to
        // 127.0.0.1 answers on every address in that block — and so does the
        // IPv6 spelling.
        var (server, client) = await BootAsync(bindHost: "0.0.0.0");
        await using (server)
        {
            using (client)
            {
                await AssertRefusedAsync(await RegisterAsync(client, url), "loopback");
            }
        }
    }

    [Theory]
    [InlineData("http://127.0.0.1:9/hook")]
    [InlineData("http://[::1]:9/hook")]
    public async Task RoutableBindWithTheKeySetAcceptsALoopbackDestination(string url)
    {
        var (server, client) = await BootAsync(
            "[webhooks]\nallow_loopback_destinations = true\n",
            bindHost: "0.0.0.0");
        await using (server)
        {
            using (client)
            {
                await AssertAcceptedAsync(await RegisterAsync(client, url));
            }
        }
    }

    [Theory]
    [InlineData("http://localhost:9/hook")]
    [InlineData("http://api.localhost:9/hook")]
    public async Task TheLocalhostNamesAreTreatedAsLoopback(string url)
    {
        var (server, client) = await BootAsync(bindHost: "0.0.0.0");
        await using (server)
        {
            using (client)
            {
                await AssertRefusedAsync(await RegisterAsync(client, url), "loopback");
            }
        }
    }

    // ── §1 what is always refused ───────────────────────────────────────────

    // Each of these is run twice: with the loopback key unset and with it set.
    // The key is about loopback and nothing else, and a guard that let it widen
    // to metadata or private space would be worse than no guard, because the
    // key's documentation would be lying.
    private const string KeyUnset = "";
    private const string KeySet = "[webhooks]\nallow_loopback_destinations = true\n";

    // …and each is run on both bind postures, because the table in §6 says "any
    // bind" for every row below. The bind only ever *widens* the guard (it is
    // one side of an OR that permits loopback, §3) and the metadata and private
    // checks run ahead of the loopback branch, so an argument can be made that
    // the routable-bind cases add nothing. That argument was in fact made — and
    // an argument is not a test. The two ways it could be wrong are both
    // realistic: a future refactor could compute the effective permission and
    // then take a different code path per posture, or the *reason* on a
    // routable bind could shift to "loopback" and hide a metadata destination
    // behind a message that reads like a configuration problem. Both are cheap
    // to pin, so they are pinned.
    private const string LoopbackBind = "127.0.0.1";
    private const string RoutableBind = "0.0.0.0";

    [Theory]
    [InlineData(KeyUnset, LoopbackBind, "http://169.254.169.254/latest/meta-data/")]
    [InlineData(KeySet, LoopbackBind, "http://169.254.169.254/latest/meta-data/")]
    [InlineData(KeyUnset, RoutableBind, "http://169.254.169.254/latest/meta-data/")]
    [InlineData(KeySet, RoutableBind, "http://169.254.169.254/latest/meta-data/")]
    [InlineData(KeyUnset, LoopbackBind, "http://100.100.100.200/latest/meta-data/")]
    [InlineData(KeySet, LoopbackBind, "http://100.100.100.200/latest/meta-data/")]
    [InlineData(KeyUnset, RoutableBind, "http://100.100.100.200/latest/meta-data/")]
    [InlineData(KeySet, RoutableBind, "http://100.100.100.200/latest/meta-data/")]
    [InlineData(KeyUnset, LoopbackBind, "http://[fd00:ec2::254]/latest/meta-data/")]
    [InlineData(KeySet, LoopbackBind, "http://[fd00:ec2::254]/latest/meta-data/")]
    [InlineData(KeyUnset, RoutableBind, "http://[fd00:ec2::254]/latest/meta-data/")]
    [InlineData(KeySet, RoutableBind, "http://[fd00:ec2::254]/latest/meta-data/")]
    public async Task CloudMetadataAddressesAreAlwaysRefused(string toml, string bindHost, string url)
    {
        // The EC2/GCP link-local, the Alibaba metadata IP, and the EC2 IPv6
        // metadata address. Reaching any of them hands out role credentials.
        var (server, client) = await BootAsync(toml, bindHost);
        await using (server)
        {
            using (client)
            {
                await AssertRefusedAsync(await RegisterAsync(client, url), "not allowed");
            }
        }
    }

    [Theory]
    [InlineData(KeyUnset, LoopbackBind)]
    [InlineData(KeySet, LoopbackBind)]
    [InlineData(KeyUnset, RoutableBind)]
    [InlineData(KeySet, RoutableBind)]
    public async Task TheGoogleMetadataHostnameIsAlwaysRefused(string toml, string bindHost)
    {
        // Refused by name, before any resolution: on GCE it resolves to
        // 169.254.169.254, but the name is also how the documented attack is
        // written and a split-horizon resolver could answer with anything.
        var (server, client) = await BootAsync(toml, bindHost);
        await using (server)
        {
            using (client)
            {
                await AssertRefusedAsync(
                    await RegisterAsync(client, "http://metadata.google.internal/computeMetadata/v1/"),
                    "not allowed");
            }
        }
    }

    [Theory]
    [InlineData(KeyUnset, LoopbackBind, "http://10.1.2.3:8080/hook")]
    [InlineData(KeySet, LoopbackBind, "http://10.1.2.3:8080/hook")]
    [InlineData(KeyUnset, RoutableBind, "http://10.1.2.3:8080/hook")]
    [InlineData(KeySet, RoutableBind, "http://10.1.2.3:8080/hook")]
    [InlineData(KeyUnset, LoopbackBind, "http://192.168.1.10:8080/hook")]
    [InlineData(KeySet, LoopbackBind, "http://192.168.1.10:8080/hook")]
    [InlineData(KeyUnset, RoutableBind, "http://192.168.1.10:8080/hook")]
    [InlineData(KeySet, RoutableBind, "http://192.168.1.10:8080/hook")]
    [InlineData(KeyUnset, LoopbackBind, "http://172.16.3.4:8080/hook")]
    [InlineData(KeySet, LoopbackBind, "http://172.16.3.4:8080/hook")]
    [InlineData(KeyUnset, RoutableBind, "http://172.16.3.4:8080/hook")]
    [InlineData(KeySet, RoutableBind, "http://172.16.3.4:8080/hook")]
    [InlineData(KeyUnset, LoopbackBind, "http://[fc00::1]:8080/hook")]
    [InlineData(KeyUnset, RoutableBind, "http://[fc00::1]:8080/hook")]
    [InlineData(KeyUnset, LoopbackBind, "http://[fe80::1]:8080/hook")]
    [InlineData(KeyUnset, RoutableBind, "http://[fe80::1]:8080/hook")]
    [InlineData(KeyUnset, LoopbackBind, "http://0.0.0.0:8080/hook")]
    [InlineData(KeyUnset, RoutableBind, "http://0.0.0.0:8080/hook")]
    [InlineData(KeyUnset, LoopbackBind, "http://[ff02::1]:8080/hook")]
    [InlineData(KeyUnset, RoutableBind, "http://[ff02::1]:8080/hook")]
    // RFC 6598 CGNAT. Refused on the *webhook* path only since the range moved
    // out of the connector guard and into the shared policy: before that, this
    // port permitted a destination Python, Go and TypeScript all refused, which
    // is why it is asserted here rather than only in the connector tests.
    [InlineData(KeyUnset, LoopbackBind, "http://100.64.0.1:8080/hook")]
    [InlineData(KeySet, LoopbackBind, "http://100.64.0.1:8080/hook")]
    [InlineData(KeyUnset, RoutableBind, "http://100.64.0.1:8080/hook")]
    [InlineData(KeySet, RoutableBind, "http://100.64.0.1:8080/hook")]
    [InlineData(KeyUnset, LoopbackBind, "http://100.64.0.0:8080/hook")]
    [InlineData(KeyUnset, LoopbackBind, "http://100.127.255.255:8080/hook")]
    public async Task PrivateLinkLocalAndReservedSpaceIsAlwaysRefused(string toml, string bindHost, string url)
    {
        var (server, client) = await BootAsync(toml, bindHost);
        await using (server)
        {
            using (client)
            {
                await AssertRefusedAsync(await RegisterAsync(client, url), "not allowed");
            }
        }
    }

    [Theory]
    [InlineData(LoopbackBind, "http://[64:ff9b::169.254.169.254]/latest/meta-data/")]
    [InlineData(RoutableBind, "http://[64:ff9b::169.254.169.254]/latest/meta-data/")]
    [InlineData(LoopbackBind, "http://[2002:a9fe:a9fe::]/latest/meta-data/")]
    [InlineData(RoutableBind, "http://[2002:a9fe:a9fe::]/latest/meta-data/")]
    [InlineData(LoopbackBind, "http://[::ffff:169.254.169.254]/latest/meta-data/")]
    [InlineData(RoutableBind, "http://[::ffff:169.254.169.254]/latest/meta-data/")]
    [InlineData(LoopbackBind, "http://[::169.254.169.254]/latest/meta-data/")]
    [InlineData(RoutableBind, "http://[::169.254.169.254]/latest/meta-data/")]
    public async Task AnIPv6AddressCarryingAnEmbeddedIPv4IsDecodedBeforeClassifying(string bindHost, string url)
    {
        // NAT64 well-known, 6to4, IPv4-mapped and the deprecated
        // IPv4-compatible form. On a NAT64 cluster the first of these reaches
        // the v4 metadata service, and classifying the wrapper as "some IPv6
        // address" would wave it straight through.
        var (server, client) = await BootAsync(bindHost: bindHost);
        await using (server)
        {
            using (client)
            {
                await AssertRefusedAsync(await RegisterAsync(client, url), "not allowed");
            }
        }
    }

    [Theory]
    [InlineData("ftp://example.com/hook")]
    [InlineData("file:///etc/passwd")]
    [InlineData("gopher://10.0.0.1/x")]
    public async Task OnlyHttpAndHttpsAreAccepted(string url)
    {
        var (server, client) = await BootAsync();
        await using (server)
        {
            using (client)
            {
                await AssertRefusedAsync(await RegisterAsync(client, url), "http");
            }
        }
    }

    // ── §5 DNS ──────────────────────────────────────────────────────────────

    /// <summary>
    /// A resolver that answers from a script, so the DNS rows below are hermetic:
    /// they never consult a real resolver, never need a network, and cannot start
    /// failing because someone else's zone changed.
    /// </summary>
    private sealed class ScriptedResolver : IHostResolver
    {
        private readonly Func<string, IReadOnlyList<IPAddress>> _answer;

        internal ScriptedResolver(Func<string, IReadOnlyList<IPAddress>> answer) => _answer = answer;

        internal static ScriptedResolver Answering(params string[] addresses) =>
            new(_ => addresses.Select(IPAddress.Parse).ToArray());

        internal static ScriptedResolver Failing() =>
            new(host => throw new System.Net.Sockets.SocketException(11001, "no such host: " + host));

        internal static ScriptedResolver Empty() => new(_ => Array.Empty<IPAddress>());

        public Task<IReadOnlyList<IPAddress>> ResolveAsync(
            string host,
            CancellationToken cancellationToken = default) =>
            Task.FromResult(_answer(host));
    }

    [Theory]
    [InlineData(LoopbackBind, "10.0.0.5")]
    [InlineData(RoutableBind, "10.0.0.5")]
    [InlineData(LoopbackBind, "169.254.169.254")]
    [InlineData(RoutableBind, "169.254.169.254")]
    [InlineData(LoopbackBind, "fd00:ec2::254")]
    [InlineData(RoutableBind, "fd00:ec2::254")]
    public async Task AHostnameResolvingIntoBlockedSpaceIsRefused(string bindHost, string answer)
    {
        // The rebinding shape: the destination looks like an innocuous name, and
        // the answer is what the server would actually connect to. Nothing about
        // the URL reveals it, so the guard has to resolve and check the answers.
        var (server, client) = await BootAsync(
            bindHost: bindHost, resolver: ScriptedResolver.Answering(answer));
        await using (server)
        {
            using (client)
            {
                await AssertRefusedAsync(await RegisterAsync(client, "http://hook.example.test/h"), "not allowed");
            }
        }
    }

    [Fact]
    public async Task EveryAddressAHostnameResolvesToIsChecked()
    {
        // A name with a public A record and a private AAAA record is only safe
        // if the guard checks all of them: connecting picks one, and it is not
        // the guard's choice which.
        var (server, client) = await BootAsync(
            resolver: ScriptedResolver.Answering("93.184.216.34", "fc00::1"));
        await using (server)
        {
            using (client)
            {
                await AssertRefusedAsync(await RegisterAsync(client, "http://hook.example.test/h"), "not allowed");
            }
        }
    }

    [Fact]
    public async Task AHostnameThatFailsToResolveIsRefused()
    {
        // Fail closed: an unresolvable name is unclassified, and unclassified
        // must never mean allowed.
        var (server, client) = await BootAsync(resolver: ScriptedResolver.Failing());
        await using (server)
        {
            using (client)
            {
                await AssertRefusedAsync(
                    await RegisterAsync(client, "http://hook.example.test/h"),
                    "could not be resolved");
            }
        }
    }

    [Fact]
    public async Task AHostnameThatResolvesToNothingIsRefused()
    {
        // An empty answer is not a successful resolution. Treating "no
        // addresses" as "no blocked addresses" is the same fail-open bug written
        // more subtly.
        var (server, client) = await BootAsync(resolver: ScriptedResolver.Empty());
        await using (server)
        {
            using (client)
            {
                await AssertRefusedAsync(
                    await RegisterAsync(client, "http://hook.example.test/h"),
                    "could not be resolved");
            }
        }
    }

    [Fact]
    public void AResolverThatNeverAnswersIsBoundedByTheResolveTimeout()
    {
        // The one row driven against the policy directly rather than through the
        // operator path: a hostile resolver that hangs is not something config
        // can express, and the timeout is what stops it stalling registration.
        // The wait is honoured even though this resolver ignores its token —
        // which is the case that matters, since a hostile resolver would.
        var policy = new WebhookEgressPolicy(
            allowLoopbackDestinations: false,
            resolver: new HangingResolver(),
            resolveTimeout: TimeSpan.FromMilliseconds(50));

        var started = System.Diagnostics.Stopwatch.StartNew();
        var decision = policy.Classify("http://hook.example.test/h");
        started.Stop();

        Assert.False(decision.Allowed);
        Assert.Contains("could not be resolved", decision.Reason, StringComparison.Ordinal);
        // Comfortably under DefaultResolveTimeout (2s): a guard that silently
        // fell back to the default instead of the 50ms passed in would still
        // finish inside 5s, so that bound alone cannot tell the two apart.
        Assert.True(started.Elapsed < TimeSpan.FromSeconds(1), $"took {started.Elapsed}");
    }

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
