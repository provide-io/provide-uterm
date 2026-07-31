//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using System.Net;
using System.Net.Http.Json;
using System.Net.WebSockets;
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
    private const string Worker = "provideshell";

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
        public required TermHub Hub { get; init; }
        public required ManualClock Clock { get; init; }
        public required ConcurrentQueue<string> Metrics { get; init; }
        public required StepWorker WorkerSocket { get; init; }
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

    private sealed class StepWorker : IWorkerWs
    {
        private int _delayNext;
        private readonly TaskCompletionSource _sendAttempted =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource _releaseSend =
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        public bool FailStep { get; set; }

        public Task SendAttempted => _sendAttempted.Task;

        public void DelayNextSend() => Interlocked.Exchange(ref _delayNext, 1);

        public void ReleaseSend() => _releaseSend.TrySetResult();

        public async Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (FailStep)
                throw new WebSocketException("worker disconnected");
            if (Interlocked.Exchange(ref _delayNext, 0) == 1)
            {
                _sendAttempted.TrySetResult();
                await _releaseSend.Task.WaitAsync(cancellationToken);
            }
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
        cfg.Sessions =
        [
            new SessionDefinition
            {
                SessionId = Worker,
                DisplayName = "Step fixture",
                AutoStart = false,
                InputMode = InputModes.Hijack,
                Visibility = "public",
            },
        ];
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "step-limit-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = ["admin"],
        });
        var hub = new TermHub(new TermHubConfig
        {
            RestAcquireRateLimitPerSec = acquireRate,
            RestSendRateLimitPerSec = sendRate,
            Clock = clock,
            OnMetric = (name, _) => metrics.Enqueue(name),
        });
        var worker = new StepWorker();
        hub.Conn.RegisterWorker(Worker, worker);
        hub.Registry.Get(Worker)!.InputMode = InputModes.Hijack;
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Clock = clock,
            Version = "step-limit",
        });
        server.Build([$"http://127.0.0.1:{port}"]);
        await server.StartAsync();
        hub.Registry.Get(Worker)!.InputMode = InputModes.Hijack;
        var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);
        var h = new Harness
        {
            Server = server,
            Http = http,
            Hub = hub,
            Clock = clock,
            Metrics = metrics,
            WorkerSocket = worker,
        };
        var acquire = await h.Acquire();
        Assert.True(
            acquire.IsSuccessStatusCode,
            $"Acquire failed with {(int)acquire.StatusCode}: {await acquire.Content.ReadAsStringAsync()}");
        h.HijackId = (await acquire.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("hijack_id").GetString()!;
        return h;
    }

    [Fact]
    public async Task ValidLeaseWithFailedWorkerDeliveryReturnsConflictWithoutSuccessEffects()
    {
        await using var h = await StartAsync(acquireRate: 50, sendRate: 50);
        h.WorkerSocket.FailStep = true;
        var beforeMetric = h.MetricCount("hijack_steps_total");

        var response = await h.Step();

        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode);
        Assert.Equal(
            "No worker connected for this session.",
            (await response.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("error").GetString());
        Assert.Equal(beforeMetric, h.MetricCount("hijack_steps_total"));
        Assert.DoesNotContain(
            h.Hub.Router.GetRecentEvents(Worker, 100),
            evt => Equals(evt.GetValueOrDefault("type"), "hijack_step"));
    }

    [Fact]
    public async Task SuccessfulDeliveryReportsLeaseAndRecordsSuccessEffects()
    {
        await using var h = await StartAsync(acquireRate: 50, sendRate: 50);

        var response = await h.Step();

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        var body = await response.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal(1600, body.GetProperty("lease_expires_at").GetDouble());
        Assert.Equal(1, h.MetricCount("hijack_steps_total"));
        Assert.Contains(
            h.Hub.Router.GetRecentEvents(Worker, 100),
            evt => Equals(evt.GetValueOrDefault("type"), "hijack_step"));
    }

    [Theory]
    [InlineData("release")]
    [InlineData("expiry")]
    [InlineData("replacement")]
    public async Task ConcurrentLifecycleTransitionWaitsForReservedStepDelivery(string transitionKind)
    {
        await using var h = await StartAsync(acquireRate: 50, sendRate: 50);
        h.WorkerSocket.DelayNextSend();

        var step = h.Step();
        await h.WorkerSocket.SendAttempted.WaitAsync(TimeSpan.FromSeconds(5));
        var transition = RunTransitionAsync();
        await Task.Delay(50);

        Assert.False(transition.IsCompleted);
        h.WorkerSocket.ReleaseSend();
        Assert.Equal(HttpStatusCode.OK, (await step).StatusCode);
        await transition;

        async Task RunTransitionAsync()
        {
            switch (transitionKind)
            {
                case "release":
                    Assert.Equal(HttpStatusCode.OK, (await h.Release()).StatusCode);
                    break;
                case "expiry":
                    h.Clock.SetMonotonic(601);
                    var expired = await h.Hub.CleanupExpiredHijackAsync(Worker);
                    Assert.True(expired.RestExpired);
                    break;
                case "replacement":
                    Assert.True(await h.Hub.Conn.RegisterWorkerAsync(Worker, new StepWorker()));
                    break;
                default:
                    throw new Xunit.Sdk.XunitException("unknown transition kind");
            }
        }
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
