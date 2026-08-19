//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Expiry that arrives while a lifecycle transition is already in flight.
//
// CleanupExpiredAsync has an arm that runs only when another transition already
// holds the worker: it enqueues a successor and waits its turn rather than
// expiring the lease out from under the transition in progress. Nothing forced
// that arm, so whether it ran at all depended on how concurrent tests happened
// to interleave. That made C# line coverage nondeterministic by six lines, which
// is what left csharp-quality-windows flipping either side of its threshold on
// identical code.
//
// Here the first transition is pinned open by ResumeFailureGate, so it is
// provably still running when the sweep looks.

using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.Server;

public sealed partial class ResumeLifecycleIntegrationTests
{
    [Fact]
    public async Task ExpiryBehindAnInFlightTransitionQueuesBehindIt()
    {
        const string workerId = "expiry-behind-transition";
        var clock = new ManualClock(100);
        clock.SetMonotonic(0);
        var hub = new TermHub(new TermHubConfig
        {
            Clock = clock,
            BrowserSendTimeout = TimeSpan.FromMilliseconds(200),
        });
        var failure = new ResumeFailureGate();
        var worker = new GatedResumeFailureWorker(failure);
        var observer = new RecordingBrowser();
        _ = NewUnstartedServer(hub, workerId, out var registry);

        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        worker.IsAuthoritative = () => ReferenceEquals(hub.Registry.Get(workerId)?.WorkerWs, worker);
        registry.MarkWorker(workerId, true, false);
        hub.Conn.RegisterBrowser(workerId, observer, "viewer");
        Assert.True((await hub.Lease.TryAcquireRestAsync(
            workerId, "rest-owner", 1, "ending-lease", clock.Monotonic())).Ok);

        // A release reserves the transition and then blocks inside the gated
        // resume, so the worker carries an incomplete DisconnectResumeCompletion.
        var releasing = hub.Lease.ReleaseRestAsync(workerId, "ending-lease");
        await failure.Attempted.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.NotNull(hub.Registry.Get(workerId)!.DisconnectResumeCompletion);

        // The lease is now expired, and the sweep runs while that release is
        // still stuck: it must queue behind it rather than tear the lease down.
        clock.SetMonotonic(2);
        var cleanup = hub.Lease.CleanupExpiredAsync(workerId);

        failure.ReleaseFault();
        _ = await releasing.WaitAsync(TimeSpan.FromSeconds(5));
        var (browserExpired, restExpired) = await cleanup.WaitAsync(TimeSpan.FromSeconds(5));

        // The release already ended REST ownership, so the sweep that queued
        // behind it finds nothing left to expire -- the point is that it waited
        // its turn and returned instead of deadlocking or double-releasing.
        Assert.False(browserExpired);
        Assert.False(restExpired);
        Assert.Null(hub.Registry.Get(workerId)!.DisconnectResumeCompletion);
    }
}
