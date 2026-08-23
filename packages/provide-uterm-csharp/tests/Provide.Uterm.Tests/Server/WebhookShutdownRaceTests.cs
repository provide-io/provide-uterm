//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;

namespace Provide.Uterm.Tests.Server;

/// <summary>
/// The two ways WebhookManager's shutdown races a delivery worker.
///
/// Both are about the same window and neither is reachable from the ordinary
/// single-threaded test: one needs the thread pool to be late, the other needs a
/// Register to land while ShutdownAsync is mid-flight.
/// </summary>
public sealed class WebhookShutdownRaceTests
{
    /// <summary>
    /// Answers OK, but parks any request to <c>/held</c> until released — and
    /// parks it IGNORING the cancellation token, which is the point: it keeps one
    /// delivery loop alive so ShutdownAsync stays parked in its Task.WhenAll and
    /// the window under test stays open for as long as the test needs.
    /// </summary>
    private sealed class GatedTransport : HttpMessageHandler
    {
        private readonly TaskCompletionSource<bool> _release = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private int _lateSends;

        internal ManualResetEventSlim Entered { get; } = new(false);

        /// <summary>Requests to anything other than /held — expected to stay 0.</summary>
        internal int LateSends => Volatile.Read(ref _lateSends);

        protected override async Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            if (request.RequestUri!.AbsolutePath == "/held")
            {
                Entered.Set();
                await _release.Task.WaitAsync(cancellationToken);
            }
            else
            {
                Interlocked.Increment(ref _lateSends);
            }

            return new HttpResponseMessage(HttpStatusCode.OK);
        }

        internal void Release() => _release.TrySetResult(true);

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                _release.TrySetResult(true);
                Entered.Dispose();
            }

            base.Dispose(disposing);
        }
    }

    private static WebhookManager BuildManager(EventBus bus, HttpMessageHandler transport) =>
        new(
            allowLoopbackDestinations: true,
            resolver: null,
            tunnelShareActive: null,
            onMetric: null,
            delivery: new WebhookDeliveryOptions
            {
                EventBus = bus,
                RetryDelays = Array.Empty<TimeSpan>(),
                Transport = transport,
            });

    private static void Publish(EventBus bus, string sessionId) =>
        bus.Enqueue(sessionId, new Dictionary<string, object?> { ["type"] = "annotation", ["seq"] = 1 });

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

    [Fact]
    public async Task ShutdownCancelsTheSharedSourceSoAWebhookRegisteredDuringItIsBornDead()
    {
        // ShutdownAsync cancels _shutdown before it releases the workers it
        // snapshotted. For those workers the cancel is redundant — Release cancels
        // each one's own linked source anyway — so deleting it changes nothing
        // that any ordinary test can see.
        //
        // It is not redundant for a webhook registered DURING the shutdown.
        // Register (Webhooks.cs) has no shutdown guard, and _shutdown.Token has
        // exactly one consumer: the linked source StartDelivery builds per worker.
        // A registration landing after the _workers snapshot but before
        // _shutdown.Dispose therefore produces a worker that the snapshot loop will
        // never release and, without the cancel, nothing else ever cancels either —
        // a delivery loop that outlives the manager that owns it.
        //
        // Holding one worker inside a cancellation-ignoring send parks ShutdownAsync
        // in Task.WhenAll, which holds that window open deterministically.
        var bus = new EventBus();
        using var transport = new GatedTransport();
        var manager = BuildManager(bus, transport);

        var held = manager.Register("held", "http://127.0.0.1:9/held", null, null, null);
        Publish(bus, "held");
        await WaitForAsync(() => transport.Entered.IsSet, "a held webhook to reach the transport");

        var shutdown = Task.Run(() => manager.ShutdownAsync());

        // The registry is cleared inside the lock that takes the snapshot, so a
        // null lookup means shutdown is past it. The short settle covers the
        // handful of statements between there and the Task.WhenAll it parks on.
        await WaitForAsync(() => manager.GetWebhook(held.WebhookId) is null, "shutdown to clear the registry");
        await Task.Delay(100);

        manager.Register("late", "http://127.0.0.1:9/late", null, null, null);
        Publish(bus, "late");
        await Task.Delay(250);

        // Born cancelled: the loop's first ReadAsync sees the cancelled token and
        // returns before it can deliver anything. Delete the CancelAsync and this
        // is 1 — the late worker is live and keeps delivering.
        Assert.Equal(0, transport.LateSends);

        transport.Release();
        await shutdown;
    }
}
