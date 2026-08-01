//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.Shell;

namespace Provide.Uterm.Tests.Server;

public sealed partial class ResumeLifecycleIntegrationTests
{
    [Fact]
    public async Task WorkerDeregistrationPreservesQueuedReplacementOrder()
    {
        var hub = new TermHub();
        var original = new RecordingWorker();
        var firstReplacement = new RecordingWorker();
        var finalReplacement = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker("replacement-clear", original));
        hub.Conn.RegisterBrowser("replacement-clear", browser, "admin");
        Assert.True(hub.Router.SetInputMode("replacement-clear", InputModes.Open).Ok);

        original.DelayNextInput();
        var server = NewUnstartedServer(hub);
        var firstInput = server.SendBrowserInputAsync(
            "replacement-clear", browser, "input-a");
        await original.InputAttempted.WaitAsync(TimeSpan.FromSeconds(5));

        var firstTransition = hub.Conn.RegisterWorkerAsync(
            "replacement-clear", firstReplacement);
        var finalTransition = hub.Conn.RegisterWorkerAsync(
            "replacement-clear", finalReplacement);
        var state = hub.Registry.Get("replacement-clear")!;
        var firstNode = state.ActiveLifecycleTransition;
        var finalNode = Assert.Single(state.LifecycleTransitionQueue);

        Assert.True(hub.Conn.DeregisterWorker("replacement-clear", original).ShouldBroadcast);
        var activeWasPreserved = ReferenceEquals(firstNode, state.ActiveLifecycleTransition);
        var queuedWasPreserved = state.LifecycleTransitionQueue.Count == 1
            && ReferenceEquals(finalNode, state.LifecycleTransitionQueue[0]);

        original.ReleaseInput();
        Assert.True(await firstInput.WaitAsync(TimeSpan.FromSeconds(5)));
        Assert.True(await firstTransition.WaitAsync(TimeSpan.FromSeconds(5)));
        Assert.True(await finalTransition.WaitAsync(TimeSpan.FromSeconds(5)));
        Assert.True(await server.SendBrowserInputAsync(
            "replacement-clear", browser, "input-i"));

        Assert.True(activeWasPreserved);
        Assert.True(queuedWasPreserved);
        Assert.Empty(firstReplacement.Inputs);
        Assert.Equal(["input-i"], finalReplacement.Inputs);
    }

    [Fact]
    public async Task ReplacementsWaitingForPauseRepairKeepFifoPriorityOverLaterInput()
    {
        const string workerId = "pause-repair-fifo";
        var hub = new TermHub();
        var original = new RecordingWorker();
        var firstReplacement = new RecordingWorker();
        var finalReplacement = new RecordingWorker();
        var owner = new RecordingBrowser();
        var viewer = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker(workerId, original));
        hub.Conn.RegisterBrowser(workerId, owner, "admin");
        hub.Conn.RegisterBrowser(workerId, viewer, "admin");
        Assert.True(hub.Router.SetInputMode(workerId, InputModes.Open).Ok);
        var pause = original.DelayNextPause();
        original.DelayNextResume();

        var acquire = hub.Lease.TryAcquireWsAsync(workerId, owner);
        await pause.Attempted.WaitAsync(TimeSpan.FromSeconds(1));
        var firstTransition = hub.Conn.RegisterWorkerAsync(workerId, firstReplacement);
        var finalTransition = hub.Conn.RegisterWorkerAsync(workerId, finalReplacement);
        var state = hub.Registry.Get(workerId)!;

        Assert.NotNull(state.ActiveLifecycleTransition);
        Assert.Single(state.LifecycleTransitionQueue);

        var server = NewUnstartedServer(hub);
        var laterInput = server.SendBrowserInputAsync(workerId, viewer, "input-i");
        Assert.False(laterInput.IsCompleted);

        pause.Release();
        Assert.True((await acquire.WaitAsync(TimeSpan.FromSeconds(1))).Ok);
        await original.ResumeAttempted.WaitAsync(TimeSpan.FromSeconds(1));

        Assert.Same(firstReplacement, state.WorkerWs);
        Assert.False(finalTransition.IsCompleted);
        Assert.False(laterInput.IsCompleted);

        original.ReleaseResume();
        Assert.True(await firstTransition.WaitAsync(TimeSpan.FromSeconds(1)));
        Assert.True(await finalTransition.WaitAsync(TimeSpan.FromSeconds(1)));
        Assert.True(await laterInput.WaitAsync(TimeSpan.FromSeconds(1)));

        Assert.Same(finalReplacement, state.WorkerWs);
        Assert.Empty(firstReplacement.Inputs);
        Assert.Equal(["input-i"], finalReplacement.Inputs);
    }

    [Fact]
    public async Task InputWaitsWhilePauseFenceHandsOffToReplacement()
    {
        const string workerId = "pause-replacement-handoff";
        var hub = new TermHub();
        var original = new RecordingWorker();
        var replacement = new RecordingWorker();
        var owner = new RecordingBrowser();
        var inputBrowser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker(workerId, original));
        hub.Conn.RegisterBrowser(workerId, owner, "admin");
        hub.Conn.RegisterBrowser(workerId, inputBrowser, "admin");
        Assert.True(hub.Router.SetInputMode(workerId, InputModes.Open).Ok);
        var pause = original.DelayNextPause();
        var continuationAttempted = new TaskCompletionSource(
            TaskCreationOptions.RunContinuationsAsynchronously);
        var continuationRelease = new TaskCompletionSource(
            TaskCreationOptions.RunContinuationsAsynchronously);
        hub.Conn.AfterReplacementPauseFenceWait = _ =>
        {
            continuationAttempted.TrySetResult();
            return continuationRelease.Task;
        };

        var acquire = hub.Lease.TryAcquireWsAsync(workerId, owner);
        await pause.Attempted.WaitAsync(TimeSpan.FromSeconds(1));
        var replacing = hub.Conn.RegisterWorkerAsync(workerId, replacement);
        pause.Release();

        await continuationAttempted.Task.WaitAsync(TimeSpan.FromSeconds(1));
        Assert.True((await acquire.WaitAsync(TimeSpan.FromSeconds(1))).Ok);
        var server = NewUnstartedServer(hub);
        var laterInput = server.SendBrowserInputAsync(
            workerId, inputBrowser, "handoff-input");

        Assert.False(laterInput.IsCompleted);
        Assert.Empty(original.Inputs);

        continuationRelease.TrySetResult();
        Assert.True(await replacing.WaitAsync(TimeSpan.FromSeconds(1)));
        Assert.True(await laterInput.WaitAsync(TimeSpan.FromSeconds(1)));
        Assert.Same(replacement, hub.Registry.Get(workerId)!.WorkerWs);
        Assert.Empty(original.Inputs);
        Assert.Equal(["handoff-input"], replacement.Inputs);
    }

    [Fact]
    public async Task QueuedReplacementsKeepFifoPriorityOverLaterOpenModeInput()
    {
        var hub = new TermHub();
        var original = new RecordingWorker();
        var firstReplacement = new RecordingWorker();
        var secondReplacement = new RecordingWorker();
        var thirdReplacement = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker("transition-fifo", original));
        hub.Conn.RegisterBrowser("transition-fifo", browser, "admin");
        Assert.True(hub.Router.SetInputMode("transition-fifo", InputModes.Open).Ok);

        original.DelayNextInput();
        var server = NewUnstartedServer(hub);
        var firstInput = server.SendBrowserInputAsync(
            "transition-fifo", browser, "input-a");
        await original.InputAttempted.WaitAsync(TimeSpan.FromSeconds(5));

        var firstTransition = hub.Conn.RegisterWorkerAsync(
            "transition-fifo", firstReplacement);
        var secondTransition = hub.Conn.RegisterWorkerAsync(
            "transition-fifo", secondReplacement);
        var thirdTransition = hub.Conn.RegisterWorkerAsync(
            "transition-fifo", thirdReplacement);
        Assert.Equal(2, QueuedLifecycleTransitionCount(
            hub.Registry.Get("transition-fifo")!));

        var laterInput = server.SendBrowserInputAsync(
            "transition-fifo", browser, "input-i");
        original.ReleaseInput();

        Assert.True(await firstInput);
        Assert.True(await firstTransition);
        Assert.True(await secondTransition);
        Assert.True(await thirdTransition);
        Assert.True(await laterInput);
        Assert.Equal(["input-a"], original.Inputs);
        Assert.Empty(firstReplacement.Inputs);
        Assert.Empty(secondReplacement.Inputs);
        Assert.Equal(["input-i"], thirdReplacement.Inputs);
    }

    [Fact]
    public async Task CancelledRebasedQueuedReplacementDoesNotStrandItsFifoSuccessor()
    {
        var hub = new TermHub();
        var original = new RecordingWorker();
        var firstReplacement = new RecordingWorker();
        var cancelledReplacement = new RecordingWorker();
        var finalReplacement = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker("transition-cancel", original));
        hub.Conn.RegisterBrowser("transition-cancel", browser, "admin");
        Assert.True(hub.Router.SetInputMode("transition-cancel", InputModes.Open).Ok);

        original.DelayNextInput();
        var server = NewUnstartedServer(hub);
        var firstInput = server.SendBrowserInputAsync(
            "transition-cancel", browser, "input-a");
        await original.InputAttempted.WaitAsync(TimeSpan.FromSeconds(5));

        var firstTransition = hub.Conn.RegisterWorkerAsync(
            "transition-cancel", firstReplacement);
        using var cancelled = new CancellationTokenSource();
        var cancelledTransition = hub.Conn.RegisterWorkerAsync(
            "transition-cancel", cancelledReplacement, cancelled.Token);
        var finalTransition = hub.Conn.RegisterWorkerAsync(
            "transition-cancel", finalReplacement);
        Assert.Equal(2, QueuedLifecycleTransitionCount(
            hub.Registry.Get("transition-cancel")!));

        Assert.True(hub.Conn.DeregisterWorker("transition-cancel", original).ShouldBroadcast);
        Assert.Equal(2, QueuedLifecycleTransitionCount(
            hub.Registry.Get("transition-cancel")!));
        cancelled.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => cancelledTransition);
        Assert.Equal(1, QueuedLifecycleTransitionCount(
            hub.Registry.Get("transition-cancel")!));

        var laterInput = server.SendBrowserInputAsync(
            "transition-cancel", browser, "input-i");
        original.ReleaseInput();

        Assert.True(await firstInput);
        Assert.True(await firstTransition);
        Assert.True(await finalTransition);
        Assert.True(await laterInput);
        Assert.Empty(cancelledReplacement.Inputs);
        Assert.Equal(["input-i"], finalReplacement.Inputs);
    }

    [Theory]
    [InlineData(false, false)]
    [InlineData(true, false)]
    [InlineData(false, true)]
    [InlineData(true, true)]
    public async Task FailedDisconnectResumeReconcilesOfflineExactlyOnce(
        bool hangs,
        bool replaceImmediately)
    {
        const string workerId = "failed-disconnect-resume";
        var ownershipChanges = new List<bool>();
        var hub = new TermHub(new TermHubConfig
        {
            BrowserSendTimeout = TimeSpan.FromMilliseconds(40),
            OnHijackChanged = (_, enabled, _) => ownershipChanges.Add(enabled),
        });
        var worker = new FaultingWorker(FaultTarget.Resume, hangs);
        var replacement = new RecordingWorker();
        var browser = new RecordingBrowser();
        var observer = new RecordingBrowser();
        _ = NewUnstartedServer(hub, workerId, out var registry);
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        registry.MarkWorker(workerId, true, false);
        worker.IsAuthoritative = () => ReferenceEquals(
            hub.Registry.Get(workerId)?.WorkerWs, worker);
        hub.Conn.RegisterBrowser(workerId, browser, "admin");
        hub.Conn.RegisterBrowser(workerId, observer, "viewer");
        Assert.True((await hub.Lease.TryAcquireWsAsync(
            workerId, browser)).Ok);

        var ownershipVersion = Assert.IsType<long>(
            hub.Conn.CleanupBrowser(workerId, browser));
        var resume = hub.Conn.ResumeWorkerIfOwnershipUnchangedAsync(
            workerId,
            ownershipVersion,
            HijackLeaseManager.ResumeFrame("dashboard", 100));
        await worker.FailureAttempted.WaitAsync(TimeSpan.FromSeconds(5));
        var replacementTransition = replaceImmediately
            ? hub.Conn.RegisterWorkerAsync(workerId, replacement)
            : null;
        worker.ReleaseThrow();

        var result = await resume.WaitAsync(TimeSpan.FromSeconds(1));
        if (replacementTransition is not null)
        {
            Assert.True(await replacementTransition.WaitAsync(TimeSpan.FromSeconds(1)));
        }

        Assert.False(result.Resumed);
        Assert.NotNull(result.Error);
        Assert.True(worker.Aborted);
        Assert.True(worker.AbortedWhileAuthoritative);
        Assert.Same(replaceImmediately ? replacement : null, hub.Registry.Get(workerId)!.WorkerWs);
        Assert.True(registry.TryGetStatus(workerId, out var offline));
        Assert.False(offline.Connected);
        Assert.Equal(SessionLifecycleState.Stopped, offline.LifecycleState);
        Assert.Equal(1, DecodeBrowserFrames(observer).Count(frame =>
            Type(frame) == "worker_disconnected"));
        Assert.Equal([true, false], ownershipChanges);

        if (replaceImmediately)
        {
            // This is the route's post-registration online mark. A replayed
            // finalizer for the failed worker must not overwrite it.
            registry.MarkWorker(workerId, true, false);
        }
        Assert.False((await hub.Conn.ReconcileWorkerDisconnectAsync(workerId, worker)).Reconciled);
        Assert.True(registry.TryGetStatus(workerId, out var afterReplay));
        Assert.Equal(replaceImmediately, afterReplay.Connected);
        Assert.Equal(1, DecodeBrowserFrames(observer).Count(frame =>
            Type(frame) == "worker_disconnected"));
        Assert.Equal([true, false], ownershipChanges);
    }

    [Theory]
    [InlineData("release", "local", false)]
    [InlineData("release", "local", true)]
    [InlineData("release", "ws", false)]
    [InlineData("release", "ws", true)]
    [InlineData("expiry", "local", false)]
    [InlineData("expiry", "local", true)]
    [InlineData("expiry", "ws", false)]
    [InlineData("expiry", "ws", true)]
    [InlineData("force", "local", false)]
    [InlineData("force", "local", true)]
    [InlineData("force", "ws", false)]
    [InlineData("force", "ws", true)]
    public async Task FailedOwnershipEndingResumeReconcilesWorkerExactlyOnce(
        string operation,
        string transport,
        bool hangs)
    {
        const string workerId = "failed-ownership-ending-resume";
        var clock = new ManualClock(100);
        clock.SetMonotonic(0);
        var ownershipChanges = new List<bool>();
        var hub = new TermHub(new TermHubConfig
        {
            Clock = clock,
            BrowserSendTimeout = TimeSpan.FromMilliseconds(200),
            OnHijackChanged = (_, enabled, _) => ownershipChanges.Add(enabled),
        });
        var failure = new ResumeFailureGate();
        var observer = new RecordingBrowser();
        var replacement = new RecordingWorker();
        _ = NewUnstartedServer(hub, workerId, out var registry);
        UshellConnector? connector = null;
        IWorkerWs worker;
        GatedResumeFailureWorker? websocketWorker = null;
        if (transport == "local")
        {
            connector = new UshellConnector(workerId, new UshellConnectorConfig
            {
                PollSleep = _ => { },
            });
            connector.Start();
            var localWorker = new LocalWorkerLink(hub, workerId, connector);
            Assert.True(await localWorker.AttachAsync(InputModes.Hijack));
            worker = localWorker;
        }
        else
        {
            websocketWorker = new GatedResumeFailureWorker(failure);
            worker = websocketWorker;
            Assert.True(hub.Conn.RegisterWorker(workerId, worker));
            websocketWorker.IsAuthoritative = () => ReferenceEquals(
                hub.Registry.Get(workerId)?.WorkerWs, worker);
        }

        try
        {
            registry.MarkWorker(workerId, true, false);
            hub.Conn.RegisterBrowser(workerId, observer, "viewer");
            Assert.True((await hub.Lease.TryAcquireRestAsync(
                workerId,
                "rest-owner",
                operation == "expiry" ? 1 : 30,
                "ending-lease",
                clock.Monotonic())).Ok);
            if (worker is LocalWorkerLink localWorker)
            {
                localWorker.SendOverride = failure.SendAsync;
            }
            if (operation == "expiry") clock.SetMonotonic(2);

            var ending = EndOwnershipAsync();
            await failure.Attempted.WaitAsync(TimeSpan.FromSeconds(1));
            var replacementTransition = hub.Conn.RegisterWorkerAsync(workerId, replacement);
            if (!hangs) failure.ReleaseFault();

            await ending.WaitAsync(TimeSpan.FromSeconds(1));
            Assert.True(await replacementTransition.WaitAsync(TimeSpan.FromSeconds(1)));
            if (hangs)
            {
                failure.ReleaseFault();
                await failure.Completed.WaitAsync(TimeSpan.FromSeconds(1));
            }

            Assert.Same(replacement, hub.Registry.Get(workerId)!.WorkerWs);
            Assert.False(Assert.IsAssignableFrom<IAbortableBrowserWs>(worker).IsActive);
            if (websocketWorker is not null)
            {
                Assert.True(websocketWorker.AbortedWhileAuthoritative);
            }
            Assert.True(registry.TryGetStatus(workerId, out var offline));
            Assert.False(offline.Connected);
            Assert.Equal(SessionLifecycleState.Stopped, offline.LifecycleState);
            Assert.Equal([true, false], ownershipChanges);
            var frames = DecodeBrowserFrames(observer);
            Assert.Equal(1, frames.Count(frame => Type(frame) == "worker_disconnected"));
            Assert.Equal(1, frames.Count(frame => Type(frame) == "hijack_state"));

            registry.MarkWorker(workerId, true, false);
            Assert.False((await hub.Conn.ReconcileWorkerDisconnectAsync(
                workerId, worker)).Reconciled);

            Assert.Same(replacement, hub.Registry.Get(workerId)!.WorkerWs);
            Assert.True(registry.TryGetStatus(workerId, out var afterReplay));
            Assert.True(afterReplay.Connected);
            Assert.Equal([true, false], ownershipChanges);
            frames = DecodeBrowserFrames(observer);
            Assert.Equal(1, frames.Count(frame => Type(frame) == "worker_disconnected"));
            Assert.Equal(1, frames.Count(frame => Type(frame) == "hijack_state"));
        }
        finally
        {
            connector?.Stop();
        }

        async Task EndOwnershipAsync()
        {
            switch (operation)
            {
                case "release":
                    Assert.True((await hub.Lease.ReleaseRestAsync(
                        workerId, "ending-lease")).Released);
                    break;
                case "expiry":
                    Assert.True((await hub.Lease.CleanupExpiredAsync(workerId)).RestExpired);
                    break;
                case "force":
                    Assert.True((await hub.Lease.ForceReleaseAsync(workerId)).Released);
                    break;
                default:
                    throw new Xunit.Sdk.XunitException("unknown ownership-ending operation");
            }
        }
    }

    [Fact]
    public async Task DeferredDashboardDisconnectPublishesAtExactOwnerClear()
    {
        const string workerId = "deferred-dashboard-disconnect";
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

        var ownershipVersion = Assert.IsType<long>(
            hub.Conn.CleanupBrowser(workerId, browser));
        var resume = hub.Conn.ResumeWorkerIfOwnershipUnchangedAsync(
            workerId,
            ownershipVersion,
            HijackLeaseManager.ResumeFrame("dashboard", 100));

        Assert.False(resume.IsCompleted);
        Assert.Equal([(true, "dashboard")], changes);
        worker.ReleaseInput();

        Assert.True(await input.WaitAsync(TimeSpan.FromSeconds(1)));
        Assert.True((await resume.WaitAsync(TimeSpan.FromSeconds(1))).Resumed);
        Assert.Equal(
            [
                (true, "dashboard"),
                (false, (string?)null),
            ],
            changes);
    }

    [Theory]
    [InlineData("rest", false)]
    [InlineData("rest", true)]
    [InlineData("browser", false)]
    [InlineData("browser", true)]
    public async Task FailedReservedInputFencesWorkerAndReleasesReplacement(
        string inputKind,
        bool hangs)
    {
        const string workerId = "failed-reserved-input";
        var ownershipLost = new TaskCompletionSource(
            TaskCreationOptions.RunContinuationsAsynchronously);
        var hub = new TermHub(new TermHubConfig
        {
            BrowserSendTimeout = TimeSpan.FromMilliseconds(40),
            OnHijackChanged = (_, enabled, _) =>
            {
                if (!enabled) ownershipLost.TrySetResult();
            },
        });
        var worker = new FaultingWorker(FaultTarget.Input, hangs);
        var replacement = new RecordingWorker();
        var browser = new RecordingBrowser();
        var server = NewUnstartedServer(hub, workerId, out var registry);
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        registry.MarkWorker(workerId, true, false);
        worker.IsAuthoritative = () => ReferenceEquals(
            hub.Registry.Get(workerId)?.WorkerWs, worker);
        hub.Conn.RegisterBrowser(workerId, browser, "admin");

        Task<(bool Ok, string Reason)>? restInput = null;
        Task<bool>? browserInput = null;
        if (inputKind == "rest")
        {
            Assert.True((await hub.Lease.TryAcquireRestAsync(
                workerId,
                "rest-owner",
                30,
                "failed-input-lease",
                hub.Clock.Monotonic())).Ok);
            restInput = hub.Conn.SendRestInputAsync(
                workerId, "failed-input-lease", "input-a");
        }
        else
        {
            Assert.True(hub.Router.SetInputMode(
                workerId, InputModes.Open).Ok);
            browserInput = server.SendBrowserInputAsync(
                workerId, browser, "input-a");
        }

        await worker.FailureAttempted.WaitAsync(TimeSpan.FromSeconds(5));
        var replacementTransition = hub.Conn.RegisterWorkerAsync(
            workerId, replacement);
        worker.ReleaseThrow();

        var inputOk = restInput is not null
            ? (await restInput.WaitAsync(TimeSpan.FromSeconds(1))).Ok
            : await browserInput!.WaitAsync(TimeSpan.FromSeconds(1));
        Assert.True(await replacementTransition.WaitAsync(TimeSpan.FromSeconds(1)));

        Assert.False(inputOk);
        Assert.True(worker.Aborted);
        Assert.True(worker.AbortedWhileAuthoritative);
        Assert.Same(replacement, hub.Registry.Get(workerId)!.WorkerWs);
        Assert.Null(hub.Registry.Get(workerId)!.HijackSession);
        Assert.True(registry.TryGetStatus(workerId, out var offline));
        Assert.False(offline.Connected);
        Assert.Equal(1, DecodeBrowserFrames(browser).Count(frame =>
            Type(frame) == "worker_disconnected"));
        registry.MarkWorker(workerId, true, false);
        Assert.False((await hub.Conn.ReconcileWorkerDisconnectAsync(workerId, worker)).Reconciled);
        Assert.True(registry.TryGetStatus(workerId, out var afterReplay));
        Assert.True(afterReplay.Connected);
        Assert.Equal(1, DecodeBrowserFrames(browser).Count(frame =>
            Type(frame) == "worker_disconnected"));
        if (inputKind == "rest")
        {
            await ownershipLost.Task.WaitAsync(TimeSpan.FromSeconds(1));
        }
    }

    [Theory]
    [InlineData("rest", "pause", false)]
    [InlineData("rest", "pause", true)]
    [InlineData("dashboard", "pause", false)]
    [InlineData("dashboard", "pause", true)]
    [InlineData("rest", "resume", false)]
    [InlineData("rest", "resume", true)]
    [InlineData("dashboard", "resume", false)]
    [InlineData("dashboard", "resume", true)]
    public async Task FailedAcquireWorkerSendReconcilesAndReleasesReplacement(
        string acquireKind,
        string failedAction,
        bool hangs)
    {
        const string workerId = "failed-acquire-send";
        var hub = new TermHub(new TermHubConfig
        {
            BrowserSendTimeout = TimeSpan.FromMilliseconds(40),
        });
        var worker = new AcquisitionFaultWorker(failedAction, hangs);
        var replacement = new RecordingWorker();
        var browser = new RecordingBrowser();
        _ = NewUnstartedServer(hub, workerId, out var registry);
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        registry.MarkWorker(workerId, true, false);
        worker.IsAuthoritative = () => ReferenceEquals(
            hub.Registry.Get(workerId)?.WorkerWs, worker);
        hub.Conn.RegisterBrowser(workerId, browser, "admin");

        var acquire = acquireKind == "rest"
            ? hub.Lease.TryAcquireRestAsync(
                workerId, "rest-owner", 30, "failed-acquire", hub.Clock.Monotonic())
            : hub.Lease.TryAcquireWsAsync(workerId, browser);

        if (failedAction == "resume")
        {
            await worker.PauseAttempted.WaitAsync(TimeSpan.FromSeconds(1));
            lock (hub.SharedLock)
            {
                var state = hub.Registry.Get(workerId)!;
                state.HijackPending = null;
                state.PendingPauseReservation = null;
                state.PendingDashboardBrowser = null;
                state.PendingDashboardOwnershipVersion = null;
            }
            worker.ReleasePause();
        }
        await worker.FailureAttempted.WaitAsync(TimeSpan.FromSeconds(1));
        var replacementTransition = hub.Conn.RegisterWorkerAsync(workerId, replacement);
        worker.ReleaseThrow();

        var result = await acquire.WaitAsync(TimeSpan.FromSeconds(1));
        Assert.True(await replacementTransition.WaitAsync(TimeSpan.FromSeconds(1)));

        Assert.False(result.Ok);
        Assert.True(worker.Aborted);
        Assert.Same(replacement, hub.Registry.Get(workerId)!.WorkerWs);
        Assert.True(registry.TryGetStatus(workerId, out var status));
        Assert.False(status.Connected);
        Assert.Null(hub.Registry.Get(workerId)!.HijackPending);
        Assert.Null(hub.Registry.Get(workerId)!.HijackOwner);
        Assert.Null(hub.Registry.Get(workerId)!.HijackSession);
    }
}
