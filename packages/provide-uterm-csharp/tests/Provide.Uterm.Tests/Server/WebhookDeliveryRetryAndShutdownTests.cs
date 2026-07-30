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

public sealed partial class WebhookDeliveryTests
{
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
