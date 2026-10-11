//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Lease operations that arrive while another lifecycle transition holds the
// worker: they queue behind it, and either run when it completes or start
// over when a worker clear retires their queued place.
//
// The transition in flight is reserved directly through the hub's own
// coordinator ("blocker"), so the queued arm is reached on every run rather
// than whenever a concurrent test happens to interleave. Everything after the
// reservation goes through the public lease APIs.

using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.Server;

public sealed partial class ResumeLifecycleIntegrationTests
{
    private static TermHub QueueHub(out ManualClock clock, Action<string, bool, string?>? onChanged = null)
    {
        clock = new ManualClock(100);
        clock.SetMonotonic(0);
        return new TermHub(new TermHubConfig { Clock = clock, OnHijackChanged = onChanged });
    }

    private static WorkerTermState HijackMode(TermHub hub, string workerId)
    {
        var st = hub.Registry.Get(workerId)!;
        st.InputMode = InputModes.Hijack;
        return st;
    }

    private static PendingLifecycleTransition Block(TermHub hub, string workerId)
    {
        lock (hub.SharedLock)
        {
            return LifecycleTransitionCoordinator.ReserveActive(hub.Registry.Get(workerId)!, "test-blocker");
        }
    }

    private static void Unblock(TermHub hub, string workerId, PendingLifecycleTransition blocker)
    {
        lock (hub.SharedLock)
        {
            LifecycleTransitionCoordinator.Complete(hub.Registry.Get(workerId)!, blocker);
        }
    }

    private static int Queued(TermHub hub, string workerId)
    {
        lock (hub.SharedLock)
        {
            return hub.Registry.Get(workerId)!.LifecycleTransitionQueue.Count;
        }
    }

    private static async Task UntilQueuedAsync(TermHub hub, string workerId, int count = 1)
    {
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while (Queued(hub, workerId) < count)
        {
            Assert.True(DateTime.UtcNow < deadline, "operation never queued");
            await Task.Delay(1);
        }
    }

    [Fact]
    public async Task QueuedRestReleaseStartsOverWhenAWorkerClearRetiresItsPlace()
    {
        const string workerId = "queued-rest-release";
        var hub = QueueHub(out var clock);
        var worker = new RecordingWorker();
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        HijackMode(hub, workerId);
        Assert.True((await hub.Lease.TryAcquireRestAsync(workerId, "op", 30, "lease-1", clock.Monotonic())).Ok);

        _ = Block(hub, workerId);
        var release = hub.Lease.ReleaseRestAsync(workerId, "lease-1");
        await UntilQueuedAsync(hub, workerId);

        // The worker leaves: its clear retires the queued release, which then
        // finds the lease already gone with the worker.
        Assert.True((await hub.Conn.ReconcileWorkerDisconnectAsync(workerId, worker)).Reconciled);
        Assert.Equal((false, false), await release.WaitAsync(TimeSpan.FromSeconds(5)));
    }

    [Fact]
    public async Task QueuedDashboardReleaseStartsOverAfterAClearAndUnwindsWhenCancelled()
    {
        const string workerId = "queued-dashboard-release";
        var hub = QueueHub(out _);
        var worker = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        HijackMode(hub, workerId);
        hub.Conn.RegisterBrowser(workerId, browser, "admin");
        Assert.True((await hub.Lease.TryAcquireWsAsync(workerId, browser)).Ok);

        // Cancelled while queued: the release gives back its queued place.
        var blocker = Block(hub, workerId);
        using (var cts = new CancellationTokenSource())
        {
            var cancelled = hub.Lease.TryReleaseWsAsync(workerId, browser, cts.Token);
            await UntilQueuedAsync(hub, workerId);
            await cts.CancelAsync();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => cancelled.WaitAsync(TimeSpan.FromSeconds(5)));
            Assert.Equal(0, Queued(hub, workerId));
        }

        // Queued again, then retired by a worker clear: it starts over and
        // finds the owner already released with the worker.
        var release = hub.Lease.TryReleaseWsAsync(workerId, browser);
        await UntilQueuedAsync(hub, workerId);
        Assert.True((await hub.Conn.ReconcileWorkerDisconnectAsync(workerId, worker)).Reconciled);
        Assert.False((await release.WaitAsync(TimeSpan.FromSeconds(5))).Released);
        _ = blocker;
    }

    [Fact]
    public async Task QueuedForceReleaseStartsOverAfterAClearAndRunsEmptyWhenActivated()
    {
        const string workerId = "queued-force-release";
        var hub = QueueHub(out var clock);
        var worker = new RecordingWorker();
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        HijackMode(hub, workerId);

        // Activated with nothing to release: it still owns (and returns) its turn.
        var blocker = Block(hub, workerId);
        var empty = hub.Lease.ForceReleaseAsync(workerId);
        await UntilQueuedAsync(hub, workerId);
        Unblock(hub, workerId, blocker);
        Assert.False((await empty.WaitAsync(TimeSpan.FromSeconds(5))).Released);
        Assert.Null(hub.Registry.Get(workerId)!.ActiveLifecycleTransition);

        // Retired by a worker clear while queued: starts over, finds nothing held.
        Assert.True((await hub.Lease.TryAcquireRestAsync(workerId, "op", 30, "lease-f", clock.Monotonic())).Ok);
        _ = Block(hub, workerId);
        var force = hub.Lease.ForceReleaseAsync(workerId);
        await UntilQueuedAsync(hub, workerId);
        Assert.True((await hub.Conn.ReconcileWorkerDisconnectAsync(workerId, worker)).Reconciled);
        Assert.False((await force.WaitAsync(TimeSpan.FromSeconds(5))).Released);
    }

    [Fact]
    public async Task QueuedExpirySweepRechecksTheLeaseWhenItsTurnComes()
    {
        const string workerId = "queued-expiry";
        var hub = QueueHub(out var clock);
        var worker = new RecordingWorker();
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        var st = HijackMode(hub, workerId);
        Assert.True((await hub.Lease.TryAcquireRestAsync(workerId, "op", 30, "lease-e", clock.Monotonic())).Ok);

        // Nothing has expired by the time the sweep's turn comes.
        var blocker = Block(hub, workerId);
        var sweep = hub.Lease.CleanupExpiredAsync(workerId);
        await UntilQueuedAsync(hub, workerId);
        Unblock(hub, workerId, blocker);
        Assert.Equal((false, false), await sweep.WaitAsync(TimeSpan.FromSeconds(5)));

        // Half expired (dashboard ownership lapsed, REST lease still live):
        // the sweep keeps the live half, both inline and when queued.
        object dashboardOwner = new();
        lock (hub.SharedLock)
        {
            st.HijackOwner = dashboardOwner;
            st.HijackOwnerExpiresAt = clock.Monotonic() - 1;
        }

        Assert.Equal((true, false), await hub.Lease.CleanupExpiredAsync(workerId));
        Assert.Null(st.HijackOwner);
        Assert.NotNull(st.HijackSession);

        lock (hub.SharedLock)
        {
            st.HijackOwner = dashboardOwner;
            st.HijackOwnerExpiresAt = clock.Monotonic() - 1;
        }

        blocker = Block(hub, workerId);
        sweep = hub.Lease.CleanupExpiredAsync(workerId);
        await UntilQueuedAsync(hub, workerId);
        Unblock(hub, workerId, blocker);
        Assert.Equal((true, false), await sweep.WaitAsync(TimeSpan.FromSeconds(5)));
        Assert.NotNull(st.HijackSession);

        // A lapsed dashboard owner whose pause is still in flight: the sweep
        // reports it but leaves the pause obligation for that reservation.
        lock (hub.SharedLock)
        {
            st.HijackSession = null;
            st.HijackOwner = dashboardOwner;
            st.HijackOwnerExpiresAt = clock.Monotonic() - 1;
            st.HijackPending = "pause-in-flight";
            st.PendingPauseReservation = "pause-in-flight";
        }

        Assert.Equal((true, false), await hub.Lease.CleanupExpiredAsync(workerId));
        Assert.Equal("pause-in-flight", st.PendingPauseObligation);
    }

    /// <summary>Accepts control frames; fails every input write; cannot be aborted either.</summary>
    private sealed class InputFailingUnabortableWorker : IAbortableBrowserWs
    {
        public bool IsActive => true;
        public int AbortAttempts { get; private set; }

        public void Abort()
        {
            AbortAttempts++;
            throw new IOException("abort failed");
        }

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            new ControlFrameDecoder().Feed(payload).OfType<ControlChunk>().Any()
                ? Task.CompletedTask
                : Task.FromException(new IOException("input refused"));
    }

    [Fact]
    public async Task FailedRestInputIsFencedEvenWhenTheTransportAbortThrows()
    {
        const string workerId = "unabortable-input";
        var hub = QueueHub(out var clock);
        var worker = new InputFailingUnabortableWorker();
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        HijackMode(hub, workerId);
        Assert.True((await hub.Lease.TryAcquireRestAsync(workerId, "op", 30, "lease-i", clock.Monotonic())).Ok);

        var (ok, _) = await hub.Lease.SendRestInputAsync(workerId, "lease-i", "x");
        Assert.False(ok);
        Assert.True(worker.AbortAttempts >= 1);
        Assert.Null(hub.Registry.Get(workerId)!.WorkerWs); // fenced by reconciliation
    }

    [Fact]
    public async Task DashboardAcquireRefusesABrowserTheHubDoesNotKnow()
    {
        const string workerId = "unknown-browser";
        var hub = QueueHub(out _);
        Assert.True(hub.Conn.RegisterWorker(workerId, new RecordingWorker()));
        HijackMode(hub, workerId);
        Assert.Equal((false, "inactive_browser"), await hub.Lease.TryAcquireWsAsync(workerId, new RecordingBrowser()));
    }

    [Fact]
    public async Task DashboardPauseThatLandsAfterItsBrowserLeftDefersToTheRestLeaseTakenMeanwhile()
    {
        const string workerId = "pause-lands-late";
        var hub = QueueHub(out var clock);
        var worker = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        HijackMode(hub, workerId);
        hub.Conn.RegisterBrowser(workerId, browser, "admin");

        var gate = worker.DelayNextPause();
        var acquire = hub.Lease.TryAcquireWsAsync(workerId, browser);
        await gate.Attempted.WaitAsync(TimeSpan.FromSeconds(5));
        hub.Conn.CleanupBrowser(workerId, browser); // frees the pending reservation
        Assert.True((await hub.Lease.TryAcquireRestAsync(workerId, "op", 30, "lease-r", clock.Monotonic())).Ok);

        gate.Release();
        Assert.Equal((false, "inactive_browser"), await acquire.WaitAsync(TimeSpan.FromSeconds(5)));
        // The REST lease stands and no resume was owed on its behalf.
        Assert.NotNull(hub.Registry.Get(workerId)!.HijackSession);
        Assert.Null(hub.Registry.Get(workerId)!.PendingPauseObligation);
    }

    [Fact]
    public void BrowserRegistrationThatFailsAfterCountingGivesBackItsQuota()
    {
        var failTelemetry = true;
        var hub = new TermHub(new TermHubConfig
        {
            MaxConnectionsPerPrincipal = 1,
            OnTelemetryEvent = (eventType, _, _, _, _) =>
            {
                if (failTelemetry && eventType == "session.registered") throw new InvalidOperationException("sink down");
            },
        });
        Assert.Throws<InvalidOperationException>(() =>
            hub.Conn.RegisterBrowser("w", new RecordingBrowser(), "viewer", principalSubjectId: "alice"));

        // The failed attempt did not keep alice's one allowed connection.
        failTelemetry = false;
        hub.Conn.RegisterBrowser("w", new RecordingBrowser(), "viewer", principalSubjectId: "alice");
    }

    [Fact]
    public async Task DeferredDisconnectResumeStopsWhenTheWorkerWasDetachedMeanwhile()
    {
        const string workerId = "deferred-disconnect-detached";
        var changes = new List<(bool Enabled, string? Owner)>();
        var hub = new TermHub(new TermHubConfig
        {
            OnHijackChanged = (_, enabled, owner) => changes.Add((enabled, owner)),
        });
        var worker = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        hub.Conn.RegisterBrowser(workerId, browser, "admin");
        Assert.True((await hub.Lease.TryAcquireWsAsync(workerId, browser)).Ok);
        worker.DelayNextInput();
        var server = NewUnstartedServer(hub);
        var input = server.SendBrowserInputAsync(workerId, browser, "deferred-input");
        await worker.InputAttempted.WaitAsync(TimeSpan.FromSeconds(1));

        var ownershipVersion = Assert.IsType<long>(hub.Conn.CleanupBrowser(workerId, browser));
        var resume = hub.Conn.ResumeWorkerIfOwnershipUnchangedAsync(
            workerId, ownershipVersion, HijackLeaseManager.ResumeFrame("dashboard", 100));
        Assert.True(hub.Conn.DisconnectWorker(workerId)); // admin detaches the worker
        worker.ReleaseInput();
        await input.WaitAsync(TimeSpan.FromSeconds(1));

        var (resumed, error) = await resume.WaitAsync(TimeSpan.FromSeconds(1));
        Assert.False(resumed);
        Assert.Null(error);
        Assert.Equal((false, (string?)null), changes[^1]); // the owner clear was still published
    }

    [Fact]
    public async Task OwnershipPublicationMatchesTheCurrentHolderKind()
    {
        const string workerId = "publication-kind";
        var changes = new List<(bool Enabled, string? Owner)>();
        var hub = QueueHub(out var clock, (_, enabled, owner) => changes.Add((enabled, owner)));
        var worker = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        HijackMode(hub, workerId);

        hub.NotifyHijackChanged(workerId, false, null);
        Assert.Equal((false, (string?)null), changes[^1]);

        Assert.True((await hub.Lease.TryAcquireRestAsync(workerId, "rest-op", 30, "lease-p", clock.Monotonic())).Ok);
        hub.NotifyHijackChanged(workerId, true, "ignored");
        Assert.Equal((true, "rest-op"), changes[^1]);
        Assert.True((await hub.Lease.ReleaseRestAsync(workerId, "lease-p")).Released);

        hub.Conn.RegisterBrowser(workerId, browser, "admin");
        Assert.True((await hub.Lease.TryAcquireWsAsync(workerId, browser)).Ok);
        var before = changes.Count;
        hub.NotifyHijackChanged(workerId, true, "ignored");
        Assert.Equal(before + 1, changes.Count);
        Assert.True(changes[^1].Enabled);
    }
}
