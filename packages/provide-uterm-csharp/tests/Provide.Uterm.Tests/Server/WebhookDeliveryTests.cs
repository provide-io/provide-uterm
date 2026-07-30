//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests.Server;

/// <summary>
/// Webhook delivery: the background loop this port did not have.
///
/// Registration used to be the whole feature — the registry recorded a URL and
/// then nothing ever POSTed to it — which had a second consequence beyond the
/// missing feature: the delivery-time half of the egress contract
/// (<c>conformance/EGRESS_GUARD.md</c> §4, plus re-classification against DNS
/// rebinding) was fully implemented and reachable from no caller, so it was
/// protecting nothing at all.
///
/// The end-to-end rows drive a real event through the production factory: HTTP
/// <c>POST /api/sessions/{id}/annotate</c> → the hub's router → the event bus →
/// the delivery worker → a real HTTP POST to a real listener. The rows about the
/// retry ladder and the auto-unregister threshold build the registry directly,
/// because those need injected retry delays (otherwise every give-up row waits
/// out the shipped 3.5-second ladder) and a resolver that changes its mind.
/// </summary>
public sealed class WebhookDeliveryTests
{
    private static int FreePort()
    {
        var listener = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    /// <summary>One received delivery.</summary>
    private sealed record Received(string Body, string? Timestamp, string? Signature, string ContentType);

    /// <summary>
    /// A real HTTP endpoint that records what it was sent and answers with a
    /// scripted status.
    /// </summary>
    /// <remarks>
    /// A real listener rather than a stub handler for the end-to-end rows,
    /// because "the server can actually reach the destination it was given" is
    /// part of what is being claimed, and a handler that never opens a socket
    /// cannot claim it. The header and signature assertions run against this too,
    /// so what is verified is the bytes that crossed a socket.
    /// </remarks>
    private sealed class Receiver : IAsyncDisposable
    {
        private readonly WebApplication _app;
        private readonly List<Received> _received = new();
        private readonly object _gate = new();
        private readonly Queue<int> _statuses;

        private Receiver(WebApplication app, int port, Queue<int> statuses)
        {
            _app = app;
            Port = port;
            _statuses = statuses;
        }

        internal int Port { get; }

        internal string Url => $"http://127.0.0.1:{Port}/hook";

        internal IReadOnlyList<Received> Deliveries
        {
            get
            {
                lock (_gate)
                {
                    return _received.ToList();
                }
            }
        }

        /// <param name="statuses">
        /// Status codes to answer with, in order; the last one repeats. Empty
        /// means always 200.
        /// </param>
        internal static async Task<Receiver> StartAsync(params int[] statuses)
        {
            var port = FreePort();
            var builder = WebApplication.CreateBuilder(new WebApplicationOptions
            {
                Args = Array.Empty<string>(),
                ApplicationName = typeof(WebhookDeliveryTests).Assembly.FullName,
            });
            builder.Logging.ClearProviders();
            builder.WebHost.UseKestrel();
            builder.WebHost.UseUrls($"http://127.0.0.1:{port}");
            var app = builder.Build();
            var receiver = new Receiver(app, port, new Queue<int>(statuses));
            app.MapPost("/hook", async ctx =>
            {
                using var reader = new StreamReader(ctx.Request.Body, Encoding.UTF8);
                var body = await reader.ReadToEndAsync();
                receiver.Record(new Received(
                    body,
                    ctx.Request.Headers["X-Uterm-Timestamp"].ToString() is { Length: > 0 } ts ? ts : null,
                    ctx.Request.Headers["X-Uterm-Signature"].ToString() is { Length: > 0 } sig ? sig : null,
                    ctx.Request.ContentType ?? ""));
                ctx.Response.StatusCode = receiver.NextStatus();
            });
            await app.StartAsync();
            return receiver;
        }

        private void Record(Received received)
        {
            lock (_gate)
            {
                _received.Add(received);
            }
        }

        private int NextStatus()
        {
            lock (_gate)
            {
                if (_statuses.Count == 0)
                {
                    return 200;
                }

                return _statuses.Count == 1 ? _statuses.Peek() : _statuses.Dequeue();
            }
        }

        /// <summary>Wait until at least <paramref name="count"/> deliveries arrived.</summary>
        internal async Task<IReadOnlyList<Received>> WaitAsync(int count = 1, int timeoutMs = 10_000)
        {
            var deadline = Environment.TickCount64 + timeoutMs;
            while (Environment.TickCount64 < deadline)
            {
                var snapshot = Deliveries;
                if (snapshot.Count >= count)
                {
                    return snapshot;
                }

                await Task.Delay(10);
            }

            Assert.Fail($"expected {count} delivery/deliveries, saw {Deliveries.Count}");
            return Array.Empty<Received>();
        }

        /// <summary>
        /// Assert nothing arrives. Bounded by a wait rather than an event,
        /// because the claim is about an absence and an absence has no signal.
        /// </summary>
        internal async Task AssertNothingDeliveredAsync(int settleMs = 750)
        {
            await Task.Delay(settleMs);
            Assert.Empty(Deliveries);
        }

        public async ValueTask DisposeAsync()
        {
            await _app.StopAsync();
            await _app.DisposeAsync();
        }
    }

    // ── end-to-end, through the production factory ──────────────────────────

    private static async Task<(UtermServer Server, HttpClient Client)> BootAsync(string toml = "")
    {
        var port = FreePort();
        var path = Path.Combine(Path.GetTempPath(), "uterm-delivery-" + Guid.NewGuid().ToString("N") + ".toml");
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

        // A loopback bind, which is what makes the loopback destination below
        // permissible at all (§3) — the receiver has to be somewhere this test
        // can reach, and that means 127.0.0.1.
        cfg.Server.Host = "127.0.0.1";
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

        var (server, _) = ServerFactory.CreateFromConfig(cfg, "delivery-tests");
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "wd-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = new[] { "admin" },
        });

        var client = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}") };
        client.DefaultRequestHeaders.Authorization =
            new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", token);
        return (server, client);
    }

    private static async Task RegisterAsync(HttpClient client, string url, string? secret = null, string? eventTypes = null)
    {
        var body = new StringBuilder("{\"url\":\"").Append(url).Append('"');
        if (secret is not null)
        {
            body.Append(",\"secret\":\"").Append(secret).Append('"');
        }

        if (eventTypes is not null)
        {
            body.Append(",\"event_types\":[").Append(eventTypes).Append(']');
        }

        body.Append('}');
        var response = await client.PostAsync(
            "/api/sessions/demo/webhooks",
            new StringContent(body.ToString(), Encoding.UTF8, "application/json"));
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
    }

    /// <summary>
    /// Produce a real session event the way an operator does: the annotate route
    /// appends an <c>annotation</c> event through the hub's router, which is what
    /// fans out to the event bus.
    /// </summary>
    private static async Task AnnotateAsync(HttpClient client, string label = "hello")
    {
        var response = await client.PostAsync(
            "/api/sessions/demo/annotate",
            new StringContent($"{{\"label\":\"{label}\"}}", Encoding.UTF8, "application/json"));
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
    }

    [Fact]
    public void WebhookConfigDefaultsToEmptyStringsRatherThanNull()
    {
        // A caller that builds a bare WebhookConfig (the registry itself always
        // fills every field, but the type is public) gets "" rather than a
        // null it would have to guard against.
        var cfg = new WebhookConfig();
        Assert.Equal("", cfg.WebhookId);
        Assert.Equal("", cfg.SessionId);
        Assert.Equal("", cfg.Url);
    }

    [Fact]
    public void WebhookIdIsAFreshUndashedGuidPerRegistration()
    {
        // "N" format specifically: the reference and Go both mint an undashed
        // 32-hex-char id, and one port spelling it "D" (with dashes) would be a
        // wire-visible divergence the first time an operator compared ids
        // across a mixed fleet.
        var manager = new WebhookManager(allowLoopbackDestinations: true);
        var cfg = manager.Register("s1", "http://127.0.0.1:9/hook", null, null, null);

        Assert.Matches("^[0-9a-f]{32}$", cfg.WebhookId);
    }

    [Fact]
    public void APatternOfExactlyTheMaxLengthIsAccepted()
    {
        // The boundary itself, not just one side of it: a mutant that loosens
        // `> 200` to `>= 200` still refuses 201 (tested elsewhere) but would
        // wrongly refuse exactly 200 too.
        var manager = new WebhookManager();
        manager.ValidatePattern(new string('a', 200));
    }

    [Fact]
    public void RegisteringWithAnOverlongPatternIsRefused()
    {
        // ValidatePattern is unit-tested directly elsewhere; this pins that
        // Register actually calls it, rather than only validating the URL.
        var manager = new WebhookManager(allowLoopbackDestinations: true);

        Assert.Throws<ArgumentException>(() =>
            manager.Register("s1", "http://127.0.0.1:9/hook", null, new string('a', 201), null));
    }

    [Fact]
    public async Task AnEventOnTheSessionIsDeliveredToTheRegisteredDestination()
    {
        // The row the whole item exists for: a registered webhook actually
        // receives something. Nothing in this port did before.
        await using var receiver = await Receiver.StartAsync();
        var (server, client) = await BootAsync();
        await using (server)
        {
            using (client)
            {
                await RegisterAsync(client, receiver.Url);

                await AnnotateAsync(client);

                var delivered = await receiver.WaitAsync();
                var payload = JsonDocument.Parse(delivered[0].Body).RootElement;
                Assert.Equal("demo", payload.GetProperty("session_id").GetString());
                Assert.NotEmpty(payload.GetProperty("webhook_id").GetString()!);
                Assert.True(payload.GetProperty("timestamp").GetDouble() > 0);
                // The event itself, not merely a notification that one happened:
                // the reference nests the whole event under `event`, and a
                // receiver that has to call back for the detail is a different
                // (worse) contract.
                var evt = payload.GetProperty("event");
                Assert.Equal("annotation", evt.GetProperty("type").GetString());
                Assert.Equal("hello", evt.GetProperty("data").GetProperty("label").GetString());
                Assert.Equal("application/json", delivered[0].ContentType);
            }
        }
    }

    [Fact]
    public async Task ASignedDeliveryCarriesHeadersTheReceiverCanVerify()
    {
        // Signing is what lets the receiver believe the payload came from this
        // server. Asserted by verifying with the shared verifier rather than by
        // re-deriving the digest here: a test that re-implements the signature
        // passes even when both sides are wrong in the same way.
        await using var receiver = await Receiver.StartAsync();
        var (server, client) = await BootAsync();
        await using (server)
        {
            using (client)
            {
                await RegisterAsync(client, receiver.Url, secret: "s3cret");

                await AnnotateAsync(client);

                var delivered = await receiver.WaitAsync();
                Assert.NotNull(delivered[0].Timestamp);
                Assert.NotNull(delivered[0].Signature);
                Assert.True(
                    WebhookSigning.VerifyWebhookSignature(
                        "s3cret",
                        Encoding.UTF8.GetBytes(delivered[0].Body),
                        delivered[0].Signature,
                        delivered[0].Timestamp),
                    "signature did not verify");
                // And the digest is over this body: a signature that verifies for
                // any body is not a signature.
                Assert.False(
                    WebhookSigning.VerifyWebhookSignature(
                        "s3cret",
                        Encoding.UTF8.GetBytes(delivered[0].Body + " "),
                        delivered[0].Signature,
                        delivered[0].Timestamp),
                    "signature verified a body it should not have");
            }
        }
    }

    [Fact]
    public async Task AnUnsignedWebhookSendsNoSignatureHeaders()
    {
        // No secret, no headers — rather than a signature under an empty key,
        // which is forgeable and which the verifier refuses anyway.
        await using var receiver = await Receiver.StartAsync();
        var (server, client) = await BootAsync();
        await using (server)
        {
            using (client)
            {
                await RegisterAsync(client, receiver.Url);

                await AnnotateAsync(client);

                var delivered = await receiver.WaitAsync();
                Assert.Null(delivered[0].Timestamp);
                Assert.Null(delivered[0].Signature);
            }
        }
    }

    [Fact]
    public async Task TheEventTypeFilterIsHonoured()
    {
        // Registering for `term` and getting annotations would make the filter
        // decorative. The positive half is the row above, so this one only has to
        // establish that a non-matching type is dropped.
        await using var receiver = await Receiver.StartAsync();
        var (server, client) = await BootAsync();
        await using (server)
        {
            using (client)
            {
                await RegisterAsync(client, receiver.Url, eventTypes: "\"term\"");

                await AnnotateAsync(client);

                await receiver.AssertNothingDeliveredAsync();
            }
        }
    }

    [Fact]
    public async Task DisposingTheServerReleasesTheDeliveryWorkers()
    {
        // Clean shutdown: after the server is gone nothing may still be POSTing
        // on its behalf. Asserted by publishing after disposal — the subscription
        // is torn down, so the event has nowhere to arrive.
        await using var receiver = await Receiver.StartAsync();
        var (server, client) = await BootAsync();
        EventBus bus;
        using (client)
        {
            await RegisterAsync(client, receiver.Url);
            await AnnotateAsync(client);
            await receiver.WaitAsync();
            bus = server.HubForTests.EventBus;
            await server.DisposeAsync();
        }

        bus.Enqueue("demo", new Dictionary<string, object?> { ["type"] = "annotation", ["seq"] = 99 });

        await Task.Delay(500);
        Assert.Single(receiver.Deliveries);
    }

    // ── the retry ladder and the kill switch ────────────────────────────────

    /// <summary>
    /// A registry wired to a real bus and a real destination, with the retry
    /// ladder shortened. The ladder is the only reason these rows are not driven
    /// through the factory: the shipped delays are 0.5s, 1s and 2s, so a row that
    /// waits out a give-up costs three and a half seconds of test time for
    /// nothing.
    /// </summary>
    /// <summary>One captured <c>(level, message)</c> pair from the manager's <c>OnLog</c> sink.</summary>
    private sealed record LogLine(string Level, string Message)
    {
        public override string ToString() => $"{Level} {Message}";
    }

    private static (
        WebhookManager Manager,
        EventBus Bus,
        List<KeyValuePair<string, long>> Metrics,
        List<LogLine> Logs) BuildRegistry(
        IReadOnlyList<TimeSpan>? retryDelays = null,
        bool allowLoopback = true,
        IHostResolver? resolver = null,
        Func<string, bool>? tunnelShareActive = null,
        EventBus? bus = null,
        TimeSpan? attemptTimeout = null,
        HttpMessageHandler? transport = null)
    {
        var metrics = new List<KeyValuePair<string, long>>();
        var logs = new List<LogLine>();
        var eventBus = bus ?? new EventBus();
        var manager = new WebhookManager(
            allowLoopbackDestinations: allowLoopback,
            resolver: resolver,
            tunnelShareActive: tunnelShareActive,
            onMetric: (name, value) =>
            {
                lock (metrics)
                {
                    metrics.Add(new KeyValuePair<string, long>(name, value));
                }
            },
            delivery: new WebhookDeliveryOptions
            {
                EventBus = eventBus,
                RetryDelays = retryDelays ?? Array.Empty<TimeSpan>(),
                AttemptTimeout = attemptTimeout,
                Transport = transport,
                OnLog = (level, message) =>
                {
                    lock (logs)
                    {
                        logs.Add(new LogLine(level, message));
                    }
                },
            });
        return (manager, eventBus, metrics, logs);
    }

    private static IReadOnlyList<LogLine> Snapshot(List<LogLine> logs)
    {
        lock (logs)
        {
            return logs.ToList();
        }
    }

    private static long Count(List<KeyValuePair<string, long>> metrics, string name)
    {
        lock (metrics)
        {
            return metrics.Where(m => m.Key == name).Sum(m => m.Value);
        }
    }

    private static async Task WaitForAsync(Func<bool> condition, string what, int timeoutMs = 10_000)
    {
        var deadline = Environment.TickCount64 + timeoutMs;
        while (Environment.TickCount64 < deadline)
        {
            if (condition())
            {
                return;
            }

            await Task.Delay(10);
        }

        Assert.Fail("timed out waiting for " + what);
    }

    private static void Publish(EventBus bus, string sessionId = "s1") =>
        bus.Enqueue(sessionId, new Dictionary<string, object?> { ["type"] = "annotation", ["seq"] = 1 });

    [Fact]
    public async Task ANonSuccessAnswerIsCountedAndRetried()
    {
        // 500, 500, then 200: two retries rather than one, so the attempt number
        // in the failure log climbs (1, then 2) rather than sitting still — the
        // only way to pin the ladder counts *up* rather than down.
        await using var receiver = await Receiver.StartAsync(500, 500, 200);
        var (manager, bus, metrics, logs) =
            BuildRegistry(retryDelays: new[] { TimeSpan.Zero, TimeSpan.Zero });
        await using (manager)
        {
            manager.Register("s1", receiver.Url, null, null, null);

            Publish(bus);

            var delivered = await receiver.WaitAsync(3);
            Assert.Equal(3, delivered.Count);
            Assert.Equal(2, Count(metrics, WebhookManager.DeliveryFailedMetric));
            Assert.Equal(0, Count(metrics, WebhookManager.DeliveryGivingUpMetric));

            var failures = Snapshot(logs).Where(l => l.Message.Contains("webhook_delivery_failed", StringComparison.Ordinal)).ToList();
            Assert.Contains(failures, l => l.Level == "warn" && l.Message.Contains("status=500 attempt=1", StringComparison.Ordinal));
            Assert.Contains(failures, l => l.Level == "warn" && l.Message.Contains("status=500 attempt=2", StringComparison.Ordinal));
        }
    }

    [Fact]
    public async Task EveryAttemptFailingGivesUp()
    {
        // The end of the ladder. One attempt here (no retry delays), so what is
        // pinned is that exhausting it is loud rather than silent.
        await using var receiver = await Receiver.StartAsync(503);
        var (manager, bus, metrics, logs) = BuildRegistry();
        await using (manager)
        {
            var cfg = manager.Register("s1", receiver.Url, null, null, null);

            Publish(bus);

            await WaitForAsync(
                () => Count(metrics, WebhookManager.DeliveryGivingUpMetric) == 1,
                "the give-up counter");
            Assert.Equal(1, Count(metrics, WebhookManager.DeliveryFailedMetric));
            Assert.Contains(
                Snapshot(logs),
                l => l.Level == "error" &&
                     l.Message == $"webhook_delivery_giving_up webhook_id={cfg.WebhookId} url={cfg.Url}");
        }
    }

    [Fact]
    public async Task AnUnreachableDestinationIsAbandonedWithoutCountingAsAFailedDelivery()
    {
        // Nothing is listening on this port. The reference distinguishes "the
        // destination answered badly" (counted) from "the destination could not be
        // reached" (logged, and only the give-up counter speaks for it), and the
        // distinction is worth keeping: one is a broken receiver, the other is a
        // broken network or a wrong URL.
        var (manager, bus, metrics, logs) = BuildRegistry();
        await using (manager)
        {
            manager.Register("s1", $"http://127.0.0.1:{FreePort()}/hook", null, null, null);

            Publish(bus);

            await WaitForAsync(
                () => Count(metrics, WebhookManager.DeliveryGivingUpMetric) == 1,
                "the give-up counter");
            Assert.Equal(0, Count(metrics, WebhookManager.DeliveryFailedMetric));
            Assert.Contains(
                Snapshot(logs),
                l => l.Level == "warn" &&
                     l.Message.Contains("webhook_delivery_error", StringComparison.Ordinal) &&
                     l.Message.Contains("attempt=1", StringComparison.Ordinal));
        }
    }

    [Fact]
    public async Task ARetryInFlightIsReleasedByShutdown()
    {
        // A long delay between attempts must not keep shutdown waiting: the
        // worker is cancelled while it is sleeping on the ladder, which is where a
        // delivery loop spends most of its life when a destination is sick.
        await using var receiver = await Receiver.StartAsync(500);
        var (manager, bus, metrics, _) = BuildRegistry(retryDelays: new[] { TimeSpan.FromMinutes(5) });
        manager.Register("s1", receiver.Url, null, null, null);
        Publish(bus);
        await receiver.WaitAsync();

        var shutdown = manager.ShutdownAsync();
        var finished = await Task.WhenAny(shutdown, Task.Delay(5_000));

        Assert.Same(shutdown, finished);
        await shutdown;
        // Proves the worker was actually asleep in the 5-minute delay rather than
        // racing ahead to a second attempt: without the delay in effect, nothing
        // would have blocked it between the first attempt and shutdown being
        // requested, and the receiver would show more than one delivery.
        Assert.Single(receiver.Deliveries);
        // A cancelled-in-flight delivery is interrupted, not abandoned: it must
        // not count as "gave up" (which would happen if the cancellation catch
        // fell through into another attempt on an already-cancelled token
        // instead of returning immediately).
        Assert.Equal(0, Count(metrics, WebhookManager.DeliveryGivingUpMetric));
    }

    [Fact]
    public async Task ShutdownIsIdempotent()
    {
        // The server shuts the registry down from DisposeAsync and a host may
        // shut it down itself; the second call must be a no-op rather than an
        // ObjectDisposedException on the way out.
        var (manager, _, _, _) = BuildRegistry();
        await manager.ShutdownAsync();
        await manager.ShutdownAsync();
        await manager.DisposeAsync();
    }

    [Fact]
    public async Task ShutdownClearsTheRegistrySoARetiredWebhookIsGone()
    {
        // Shutdown does not just stop the workers, it clears the registry: a
        // webhook cannot answer to GetWebhook once the manager that owned it has
        // torn down.
        var (manager, _, _, _) = BuildRegistry();
        var cfg = manager.Register("s1", "http://127.0.0.1:9/hook", null, null, null);

        await manager.ShutdownAsync();

        Assert.Null(manager.GetWebhook(cfg.WebhookId));
    }

    [Fact]
    public async Task AttemptTimeoutShorterThanTheDefaultIsHonoured()
    {
        // The custom-timeout seam: a destination that never answers has to be
        // abandoned on the *configured* timeout, not the shipped 5-second
        // default — otherwise a caller-supplied short timeout is silently
        // ignored and every stuck destination costs 5 seconds regardless.
        var (manager, bus, metrics, _) = BuildRegistry(
            attemptTimeout: TimeSpan.FromMilliseconds(50),
            transport: new HangingHandler());
        await using (manager)
        {
            // A literal so registration needs no resolver at all — only the
            // delivery-time attempt itself is under test.
            manager.Register("s1", "https://93.184.216.34/h", null, null, null);

            var started = System.Diagnostics.Stopwatch.StartNew();
            Publish(bus);

            await WaitForAsync(
                () => Count(metrics, WebhookManager.DeliveryGivingUpMetric) == 1,
                "the give-up counter",
                timeoutMs: 4_000);
            started.Stop();

            // Comfortably under DefaultAttemptTimeout (5s): a guard that fell
            // back to the default instead of the 50ms passed in would also
            // finish inside 4s of wall clock in a WaitForAsync poll, so the
            // bound has to sit well below the default to distinguish the two.
            Assert.True(started.Elapsed < TimeSpan.FromSeconds(2), $"took {started.Elapsed}");
        }
    }

    /// <summary>
    /// A handler that never answers on its own — only <see cref="AttemptAsync"/>'s
    /// per-attempt <c>CancelAfter</c> can end the wait, which is the point:
    /// hooking the token here is what makes the test observe *that* timeout
    /// rather than hanging forever regardless of what the product code does.
    /// </summary>
    private sealed class HangingHandler : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            var tcs = new TaskCompletionSource<HttpResponseMessage>();
            cancellationToken.Register(() => tcs.TrySetCanceled(cancellationToken));
            return tcs.Task;
        }
    }

    /// <summary>
    /// A transport that never answers on its own <em>and ignores cancellation</em> —
    /// unlike <see cref="HangingHandler"/>, which is exactly what makes a worker
    /// genuinely stuck mid-send rather than merely slow. Some real transports
    /// behave this way (a socket read already past the point a cancellation can
    /// unwind it), and it is the only way to make <c>ShutdownAsync</c>'s
    /// <c>Task.WhenAll(workers.Select(w => w.Loop))</c> wait observable: a
    /// handler that itself honours cancellation would let the worker exit
    /// quickly whether or not that wait is there at all.
    /// </summary>
    private sealed class NeverCancelsHandler : HttpMessageHandler
    {
        private readonly TaskCompletionSource _started = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource<HttpResponseMessage> _release =
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        internal Task Started => _started.Task;

        internal void Release(HttpResponseMessage response) => _release.TrySetResult(response);

        protected override async Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            _started.TrySetResult();
            return await _release.Task.ConfigureAwait(false);
        }
    }

    [Fact]
    public async Task ShutdownWaitsForAWorkerStuckInAnInFlightSendBeforeReturning()
    {
        // Pins ShutdownAsync's Task.WhenAll(...) itself: without it, ShutdownAsync
        // would return the instant it cancels and releases the workers, even
        // while one is still mid-POST — the fault would just be logged by the
        // loop later, unobserved by the caller who thought teardown was done.
        var handler = new NeverCancelsHandler();
        var (manager, bus, _, _) = BuildRegistry(transport: handler);
        manager.Register("s1", "http://127.0.0.1:9/hook", null, null, null);
        Publish(bus);

        await handler.Started.WaitAsync(TimeSpan.FromSeconds(5));

        var shutdownTask = manager.ShutdownAsync();
        await Task.Delay(200);
        Assert.False(shutdownTask.IsCompleted, "ShutdownAsync returned before the in-flight send finished");

        handler.Release(new HttpResponseMessage(HttpStatusCode.OK));

        await shutdownTask.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.True(shutdownTask.IsCompleted);
    }

    // ── the delivery-time guard, now that something calls it ────────────────

    /// <summary>
    /// A resolver that answers safely once and dangerously afterwards — the
    /// rebinding shape, which is the reason the guard runs again at delivery
    /// rather than trusting the registration-time verdict.
    /// </summary>
    private sealed class RebindingResolver : IHostResolver
    {
        private int _calls;

        public Task<IReadOnlyList<IPAddress>> ResolveAsync(
            string host,
            CancellationToken cancellationToken = default)
        {
            var first = Interlocked.Increment(ref _calls) == 1;
            IReadOnlyList<IPAddress> answer = new[]
            {
                IPAddress.Parse(first ? "93.184.216.34" : "169.254.169.254"),
            };
            return Task.FromResult(answer);
        }
    }

    [Fact]
    public async Task ADestinationThatGoesBadIsRefusedAtDeliveryAndEventuallyRetired()
    {
        // Registration passes (the name answered with a public address), and then
        // the name starts answering with the metadata IP. Every delivery
        // re-classifies, so the refusal is caught — and after three consecutive
        // refusals the webhook is retired rather than re-evaluated forever on
        // whatever schedule the event source runs at.
        var (manager, bus, metrics, logs) = BuildRegistry(resolver: new RebindingResolver());
        await using (manager)
        {
            var cfg = manager.Register("s1", "http://hook.example.test/h", null, null, null);

            for (var i = 0; i < WebhookManager.MaxBlockedDeliveries; i++)
            {
                Publish(bus);
                await WaitForAsync(
                    () => Count(metrics, WebhookManager.DeliveryBlockedMetric) == i + 1,
                    $"blocked delivery {i + 1}");
                // The count in the log has to climb with the counter, not just
                // exist: it is what an operator reads to tell "just started
                // failing" from "one strike from being retired".
                Assert.Contains(
                    Snapshot(logs),
                    l => l.Level == "warn" &&
                         l.Message == $"webhook_delivery_blocked webhook_id={cfg.WebhookId} url={cfg.Url} " +
                             $"reason=unsafe_destination count={i + 1}");
            }

            await WaitForAsync(
                () => Count(metrics, WebhookManager.AutoUnregisteredMetric) == 1,
                "the auto-unregister counter");
            Assert.Null(manager.GetWebhook(cfg.WebhookId));
            Assert.Contains(
                Snapshot(logs),
                l => l.Level == "error" &&
                     l.Message == $"webhook_auto_unregistered webhook_id={cfg.WebhookId} url={cfg.Url} " +
                         $"reason=ssrf_guard_threshold count={WebhookManager.MaxBlockedDeliveries}");
        }
    }

    [Fact]
    public async Task ATunnelSharedSessionNeverRetiresItsWebhook()
    {
        // §4 through the live loop, which is where the counter split actually
        // matters: well past the threshold, the webhook is still registered. A
        // share is revocable, so suppressing deliveries while it is live must not
        // be the same thing as deciding the destination is bad.
        await using var receiver = await Receiver.StartAsync();
        var (manager, bus, metrics, logs) = BuildRegistry(tunnelShareActive: _ => true);
        await using (manager)
        {
            var cfg = manager.Register("s1", receiver.Url, null, null, null);

            for (var i = 0; i < WebhookManager.MaxBlockedDeliveries + 2; i++)
            {
                Publish(bus);
                await WaitForAsync(
                    () => Count(metrics, WebhookManager.DeliveryBlockedTunnelMetric) == i + 1,
                    $"tunnel-blocked delivery {i + 1}");
            }

            Assert.Equal(0, Count(metrics, WebhookManager.DeliveryBlockedMetric));
            Assert.Equal(0, Count(metrics, WebhookManager.AutoUnregisteredMetric));
            Assert.NotNull(manager.GetWebhook(cfg.WebhookId));
            Assert.Empty(receiver.Deliveries);
            // The dedicated log line (§4), distinct from the generic blocked-
            // destination one above: an operator grepping for it must be able to
            // tell "suppressed while shared" from "destination gone bad".
            Assert.Contains(
                Snapshot(logs),
                l => l.Level == "warn" &&
                     l.Message == $"webhook_delivery_blocked webhook_id={cfg.WebhookId} url={cfg.Url} " +
                         "session_id=s1 reason=loopback_destination_while_tunnel_shared");
        }
    }

    /// <summary>A resolver whose answer is whatever the test last set.</summary>
    private sealed class SwitchableResolver : IHostResolver
    {
        internal string Answer { get; set; } = "93.184.216.34";

        public Task<IReadOnlyList<IPAddress>> ResolveAsync(
            string host,
            CancellationToken cancellationToken = default) =>
            Task.FromResult<IReadOnlyList<IPAddress>>(new[] { IPAddress.Parse(Answer) });
    }

    [Fact]
    public async Task AGuardPassClearsTheConsecutiveRefusalTally()
    {
        // Consecutive, not cumulative. A destination that is intermittently
        // unresolvable — or behind a resolver that occasionally answers oddly —
        // would otherwise accumulate strikes over hours and be retired for a
        // problem it no longer has.
        var resolver = new SwitchableResolver();
        var (manager, bus, metrics, _) = BuildRegistry(resolver: resolver);
        await using (manager)
        {
            var cfg = manager.Register("s1", "http://hook.example.test/h", null, null, null);

            resolver.Answer = "10.0.0.5";
            Publish(bus);
            await WaitForAsync(() => Count(metrics, WebhookManager.DeliveryBlockedMetric) == 1, "refusal 1");
            Publish(bus);
            await WaitForAsync(() => Count(metrics, WebhookManager.DeliveryBlockedMetric) == 2, "refusal 2");

            // One guard pass in the middle resets the tally. The POST that follows
            // it goes nowhere — the destination name does not resolve for the HTTP
            // client, which is a different question from the guard's — so the
            // give-up counter is the observable proof the guard said yes.
            resolver.Answer = "127.0.0.1";
            Publish(bus);
            await WaitForAsync(
                () => Count(metrics, WebhookManager.DeliveryGivingUpMetric) == 1,
                "the delivery that the guard allowed");

            // …so two more refusals still do not reach three in a row.
            resolver.Answer = "10.0.0.5";
            Publish(bus);
            await WaitForAsync(() => Count(metrics, WebhookManager.DeliveryBlockedMetric) == 3, "refusal 3");
            Publish(bus);
            await WaitForAsync(() => Count(metrics, WebhookManager.DeliveryBlockedMetric) == 4, "refusal 4");

            Assert.Equal(0, Count(metrics, WebhookManager.AutoUnregisteredMetric));
            Assert.NotNull(manager.GetWebhook(cfg.WebhookId));
        }
    }

    // ── worker lifecycle ────────────────────────────────────────────────────

    [Fact]
    public async Task UnregisteringStopsTheWorker()
    {
        await using var receiver = await Receiver.StartAsync();
        var (manager, bus, _, _) = BuildRegistry();
        await using (manager)
        {
            var cfg = manager.Register("s1", receiver.Url, null, null, null);
            Publish(bus);
            await receiver.WaitAsync();

            Assert.True(manager.Unregister(cfg.WebhookId));

            Publish(bus);
            await Task.Delay(500);
            Assert.Single(receiver.Deliveries);
        }
    }

    [Fact]
    public async Task UnregisteringSomethingUnknownIsFalse()
    {
        var (manager, _, _, _) = BuildRegistry();
        await using (manager)
        {
            Assert.False(manager.Unregister("no-such-webhook"));
        }
    }

    [Fact]
    public async Task TheWorkerDisconnectSentinelEndsTheWorker()
    {
        // The bus signals a departed worker with a null event. The delivery loop
        // has to treat that as end-of-stream rather than as an event to POST.
        await using var receiver = await Receiver.StartAsync();
        var (manager, bus, _, _) = BuildRegistry();
        await using (manager)
        {
            manager.Register("s1", receiver.Url, null, null, null);

            bus.CloseWorker("s1");

            // CloseWorker also drops the subscription, so this goes nowhere.
            Publish(bus);
            await receiver.AssertNothingDeliveredAsync();
        }
    }

    [Fact]
    public async Task AWorkerThatThrowsIsLoggedRatherThanLost()
    {
        // A delivery worker is fire-and-forget, so an exception inside it has no
        // caller to surface at. Unobserved, "webhooks silently stopped" is the
        // failure mode; the loop therefore catches and logs, and this pins that
        // it does. The tunnel predicate is the injected seam nearest the top of a
        // delivery, so it is the easiest thing to break on purpose.
        var logged = new List<string>();
        var bus = new EventBus();
        var manager = new WebhookManager(
            allowLoopbackDestinations: true,
            tunnelShareActive: _ => throw new InvalidOperationException("tunnel store exploded"),
            delivery: new WebhookDeliveryOptions
            {
                EventBus = bus,
                OnLog = (level, message) =>
                {
                    lock (logged)
                    {
                        logged.Add(level + " " + message);
                    }
                },
            });
        await using (manager)
        {
            manager.Register("s1", "http://127.0.0.1:9/hook", null, null, null);

            Publish(bus);

            await WaitForAsync(
                () =>
                {
                    lock (logged)
                    {
                        return logged.Any(l => l.Contains("webhook_delivery_loop_failed", StringComparison.Ordinal));
                    }
                },
                "the loop-failure log line");
        }
    }

    [Fact]
    public async Task AnEmbedderWithNoEventSourceGetsAnInertWebhook()
    {
        // No bus is a graceful no-op, not an error: a host using the registry for
        // its REST semantics alone has nothing to subscribe to, and the reference
        // treats a missing bus the same way.
        var manager = new WebhookManager(allowLoopbackDestinations: true);
        await using (manager)
        {
            var cfg = manager.Register("s1", "http://127.0.0.1:9/hook", null, null, null);

            Assert.NotNull(manager.GetWebhook(cfg.WebhookId));
            Assert.True(manager.Unregister(cfg.WebhookId));
        }
    }

    [Fact]
    public async Task TheInjectedTransportIsUsedWhenOneIsSupplied()
    {
        // The handler seam, which is what a host wiring its own outbound policy
        // (proxy, mTLS, connection limits) would supply. Exercised once so it is
        // not shipped untried.
        var seen = new List<HttpRequestMessage>();
        var bus = new EventBus();
        var handler = new RecordingHandler(seen);
        var manager = new WebhookManager(
            allowLoopbackDestinations: true,
            delivery: new WebhookDeliveryOptions
            {
                EventBus = bus,
                Transport = handler,
                Now = () => 1_700_000_000,
            });
        await using (manager)
        {
            manager.Register("s1", "http://127.0.0.1:9/hook", null, null, "sekret");

            Publish(bus);

            await WaitForAsync(
                () =>
                {
                    lock (seen)
                    {
                        return seen.Count == 1;
                    }
                },
                "the recorded request");
            Assert.Equal("1700000000", seen[0].Headers.GetValues("X-Uterm-Timestamp").Single());
        }

        // A caller-supplied handler is not this manager's to dispose: it may be
        // shared with other clients the caller owns. `disposeHandler: false` on
        // the internal HttpClient is what keeps shutdown from taking someone
        // else's handler down with it.
        Assert.False(handler.Disposed);
    }

    private sealed class RecordingHandler : HttpMessageHandler
    {
        private readonly List<HttpRequestMessage> _seen;

        internal RecordingHandler(List<HttpRequestMessage> seen) => _seen = seen;

        internal bool Disposed { get; private set; }

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            lock (_seen)
            {
                _seen.Add(request);
            }

            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.NoContent));
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                Disposed = true;
            }

            base.Dispose(disposing);
        }
    }
}
