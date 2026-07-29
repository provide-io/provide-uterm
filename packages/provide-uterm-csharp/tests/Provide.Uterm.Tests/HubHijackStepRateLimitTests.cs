//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// <c>POST /worker/{id}/hijack/{hijack_id}/step</c> single-steps a paused
/// worker, so it is a write path into somebody else's terminal and is metered
/// like one. The reference charges it against the <em>send</em> budget —
/// <c>bridge/routes/rest.py:429</c> calls <c>hub.allow_rest_send_for</c>, the
/// same bucket <c>hijack_send</c> spends at <c>:345</c> — while counting the
/// refusal under its own name, <c>rest_step_rate_limited_total</c>
/// (<c>rest.py:430</c>). Go agrees: <c>server/bridge_rest2.go:97</c>.
///
/// Three properties are asserted here because each one is separately losable:
/// the shared budget (step and send drain one bucket, acquire keeps its own),
/// the distinct metric, and the <em>position</em> of the check — after
/// authn/authz and before the lease lookup, so an over-budget caller is told
/// 429 and never learns whether the lease id they guessed exists.
///
/// Time is driven by <see cref="ManualClock"/>, which every hub bucket reads
/// (<c>TermHubConfig.Clock</c> → <c>RateLimiter</c> → <c>TokenBucket</c>), so a
/// refill is a <c>SetMonotonic</c> call rather than a sleep and a prayer.
/// </summary>
public sealed class HubHijackStepRateLimitTests
{
    private const string Worker = "provide-shell";

    private static int FreePort()
    {
        var listener = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private sealed class Harness : IAsyncDisposable
    {
        public required UtermServer Server { get; init; }
        public required HttpClient Http { get; init; }
        public required ManualClock Clock { get; init; }
        public required ConcurrentQueue<string> Metrics { get; init; }
        public string HijackId { get; set; } = "";

        public int MetricCount(string name) => Metrics.Count(m => string.Equals(m, name, StringComparison.Ordinal));

        public Task<HttpResponseMessage> Step(string? hijackId = null) =>
            Http.PostAsync($"/worker/{Worker}/hijack/{hijackId ?? HijackId}/step", Json("{}"));

        public Task<HttpResponseMessage> Send(string keys = "x") =>
            Http.PostAsync($"/worker/{Worker}/hijack/{HijackId}/send", Json($$"""{"keys":"{{keys}}"}"""));

        public Task<HttpResponseMessage> Acquire() =>
            Http.PostAsync($"/worker/{Worker}/hijack/acquire", Json("""{"owner":"tester","lease_s":600}"""));

        public Task<HttpResponseMessage> Release() =>
            Http.PostAsync($"/worker/{Worker}/hijack/{HijackId}/release", Json("{}"));

        public async ValueTask DisposeAsync()
        {
            Http.Dispose();
            await Server.DisposeAsync().ConfigureAwait(false);
        }
    }

    private static StringContent Json(string body) => new(body, Encoding.UTF8, "application/json");

    /// <summary>
    /// A started server holding one live lease, with both REST budgets sized by
    /// the caller and a manual clock wired through the hub's limiter.
    /// </summary>
    private static async Task<Harness> StartAsync(double acquireRate, double sendRate)
    {
        var clock = new ManualClock(wall: 1000);
        var metrics = new ConcurrentQueue<string>();
        var cfg = UtermServerConfig.Default();
        var port = FreePort();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "step-limit-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = ["admin"],
        });
        var server = new UtermServer(new ServerDeps
        {
            Hub = new TermHub(new TermHubConfig
            {
                RestAcquireRateLimitPerSec = acquireRate,
                RestSendRateLimitPerSec = sendRate,
                Clock = clock,
                OnMetric = (name, _) => metrics.Enqueue(name),
            }),
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Clock = clock,
            Version = "step-limit",
        });
        server.Build([$"http://127.0.0.1:{port}"]);
        await server.StartAsync();
        var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);
        var h = new Harness { Server = server, Http = http, Clock = clock, Metrics = metrics };
        (await http.PostAsync($"/api/sessions/{Worker}/mode", Json("""{"input_mode": "hijack"}""")))
            .EnsureSuccessStatusCode();
        var acquire = await h.Acquire();
        acquire.EnsureSuccessStatusCode();
        h.HijackId = (await acquire.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("hijack_id").GetString()!;
        return h;
    }

    /// <summary>The refusal itself: 429, that body, and nothing else.</summary>
    private static async Task AssertRateLimited(HttpResponseMessage response)
    {
        Assert.Equal(HttpStatusCode.TooManyRequests, response.StatusCode);
        Assert.Equal("""{"error":"rate_limited"}""", await response.Content.ReadAsStringAsync());
        Assert.Empty(response.Headers.RetryAfter?.ToString() ?? "");
        Assert.DoesNotContain(
            response.Headers.Concat(response.Content.Headers),
            h => h.Key.StartsWith("X-RateLimit", StringComparison.OrdinalIgnoreCase));
    }

    [Fact]
    public async Task A_Step_Flood_Is_Refused_Once_The_Budget_Is_Spent()
    {
        await using var h = await StartAsync(acquireRate: 50, sendRate: 2);

        Assert.Equal(HttpStatusCode.OK, (await h.Step()).StatusCode);
        Assert.Equal(HttpStatusCode.OK, (await h.Step()).StatusCode);
        await AssertRateLimited(await h.Step());

        // The refusal is counted under step's own name, not send's or acquire's.
        Assert.Equal(1, h.MetricCount("rest_step_rate_limited_total"));
        Assert.Equal(0, h.MetricCount("rest_send_rate_limited_total"));
        Assert.Equal(0, h.MetricCount("rest_acquire_rate_limited_total"));

        // Refill is a clock step, not a sleep: 10s at 2/s refills the burst.
        h.Clock.SetMonotonic(10);
        Assert.Equal(HttpStatusCode.OK, (await h.Step()).StatusCode);
    }

    [Fact]
    public async Task Step_And_Send_Drain_One_Shared_Budget()
    {
        await using var h = await StartAsync(acquireRate: 50, sendRate: 2);

        // A send flood leaves no step budget…
        Assert.Equal(HttpStatusCode.OK, (await h.Send()).StatusCode);
        Assert.Equal(HttpStatusCode.OK, (await h.Send()).StatusCode);
        await AssertRateLimited(await h.Send());
        Assert.Equal(1, h.MetricCount("rest_send_rate_limited_total"));
        await AssertRateLimited(await h.Step());
        Assert.Equal(1, h.MetricCount("rest_step_rate_limited_total"));

        // …and after a refill, a step flood leaves no send budget.
        h.Clock.SetMonotonic(10);
        Assert.Equal(HttpStatusCode.OK, (await h.Step()).StatusCode);
        Assert.Equal(HttpStatusCode.OK, (await h.Step()).StatusCode);
        await AssertRateLimited(await h.Send());
        Assert.Equal(2, h.MetricCount("rest_send_rate_limited_total"));
    }

    [Fact]
    public async Task Acquire_Keeps_A_Budget_Of_Its_Own()
    {
        await using var h = await StartAsync(acquireRate: 50, sendRate: 1);

        Assert.Equal(HttpStatusCode.OK, (await h.Step()).StatusCode);
        await AssertRateLimited(await h.Step());

        // Send budget is gone; the acquire bucket is untouched, so re-leasing works.
        Assert.Equal(HttpStatusCode.OK, (await h.Release()).StatusCode);
        var again = await h.Acquire();
        Assert.Equal(HttpStatusCode.OK, again.StatusCode);
        Assert.Equal(0, h.MetricCount("rest_acquire_rate_limited_total"));
    }

    [Fact]
    public async Task An_Over_Budget_Step_At_An_Unknown_Lease_Answers_429_Not_404()
    {
        await using var h = await StartAsync(acquireRate: 50, sendRate: 2);
        const string Unknown = "deadbeefdeadbeefdeadbeefdeadbeef";

        // In budget, an unknown-but-well-formed lease id is a 404 — that is the
        // answer the limiter must hide once the caller is over budget.
        var known = await h.Step(Unknown);
        Assert.Equal(HttpStatusCode.NotFound, known.StatusCode);

        Assert.Equal(HttpStatusCode.OK, (await h.Step()).StatusCode);
        // Budget spent (one 404'd step + one real step): the enumeration attempt
        // is refused before the lease is looked up.
        await AssertRateLimited(await h.Step(Unknown));
    }
}
