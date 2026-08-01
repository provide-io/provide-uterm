//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.Server;

public sealed partial class ResumeLifecycleIntegrationTests
{
    [Fact]
    public async Task DelayedDisconnectPublicationCannotOverwriteReplacementOwnership()
    {
        const string workerId = "disconnect-publication-race";
        var changes = new List<bool>();
        var changesGate = new object();
        var hub = new TermHub(new TermHubConfig
        {
            BrowserSendTimeout = TimeSpan.FromSeconds(5),
            OnHijackChanged = (_, enabled, _) =>
            {
                lock (changesGate) changes.Add(enabled);
            },
        });
        var original = new RecordingWorker();
        var replacement = new RecordingWorker();
        var owner = new RecordingBrowser();
        var delayedObserver = new DelayedDisconnectBrowser();
        _ = NewUnstartedServer(hub, workerId, out var registry);
        Assert.True(hub.Conn.RegisterWorker(workerId, original));
        registry.MarkWorker(workerId, true, false);
        hub.Conn.RegisterBrowser(workerId, owner, "admin");
        hub.Conn.RegisterBrowser(workerId, delayedObserver, "viewer");

        var teardown = hub.Conn.ReconcileWorkerDisconnectAsync(workerId, original);
        await delayedObserver.DisconnectAttempted.WaitAsync(TimeSpan.FromSeconds(1));

        Assert.True(await hub.Conn.RegisterWorkerAsync(workerId, replacement));
        registry.MarkWorker(workerId, true, false);
        Assert.True((await hub.Lease.TryAcquireWsAsync(workerId, owner)).Ok);
        await hub.BroadcastHijackStateAsync(workerId);

        delayedObserver.ReleaseDisconnect();
        Assert.True((await teardown.WaitAsync(TimeSpan.FromSeconds(1))).Reconciled);

        lock (changesGate) Assert.Equal([true], changes);
        var frames = DecodeBrowserFrames(delayedObserver);
        Assert.Equal(1, frames.Count(frame => Type(frame) == "worker_disconnected"));
        var disconnectedIndex = Array.FindIndex(
            frames.ToArray(), frame => Type(frame) == "worker_disconnected");
        var hijackStates = frames
            .Select((frame, index) => (Frame: frame, Index: index))
            .Where(item => Type(item.Frame) == "hijack_state")
            .ToArray();
        Assert.NotEmpty(hijackStates);
        Assert.All(hijackStates, item => Assert.True(Bool(item.Frame, "hijacked")));
        Assert.All(hijackStates, item => Assert.True(item.Index > disconnectedIndex));
    }

    [Theory]
    [InlineData("acquire")]
    [InlineData("release")]
    [InlineData("force")]
    public async Task DelayedOwnershipPublicationCannotOverwriteSuccessor(string operation)
    {
        const string workerId = "delayed-ownership-publication";
        var changes = new List<bool>();
        var hub = new TermHub(new TermHubConfig
        {
            OnHijackChanged = (_, enabled, _) => changes.Add(enabled),
        });
        var worker = new RecordingWorker();
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));

        Assert.True((await hub.Lease.TryAcquireRestAsync(
            workerId, "first-owner", 30, "first-hijack", hub.Clock.Monotonic())).Ok);
        var state = hub.Registry.Get(workerId)!;
        OwnershipPublicationToken delayed;
        switch (operation)
        {
            case "acquire":
                delayed = OwnershipPublicationToken.RestHeld(
                    workerId,
                    state.HijackOwnershipVersion,
                    "first-hijack",
                    "first-owner");
                Assert.True((await hub.Lease.ReleaseRestAsync(
                    workerId, "first-hijack")).Released);
                break;
            case "release":
                Assert.True((await hub.Lease.ReleaseRestAsync(
                    workerId, "first-hijack")).Released);
                delayed = OwnershipPublicationToken.Released(
                    workerId, state.HijackOwnershipVersion);
                Assert.True((await hub.Lease.TryAcquireRestAsync(
                    workerId,
                    "successor-owner",
                    30,
                    "successor-hijack",
                    hub.Clock.Monotonic())).Ok);
                break;
            case "force":
                Assert.True((await hub.Lease.ForceReleaseAsync(workerId)).Released);
                delayed = OwnershipPublicationToken.Released(
                    workerId, state.HijackOwnershipVersion);
                Assert.True((await hub.Lease.TryAcquireRestAsync(
                    workerId,
                    "successor-owner",
                    30,
                    "successor-hijack",
                    hub.Clock.Monotonic())).Ok);
                break;
            default:
                throw new Xunit.Sdk.XunitException("unknown operation");
        }

        changes.Clear();
        Assert.False(hub.State.NotifyHijackChanged(delayed));
        Assert.Empty(changes);
    }

    [Theory]
    [InlineData("acquire")]
    [InlineData("disconnect")]
    [InlineData("release")]
    [InlineData("restore")]
    public async Task DelayedDashboardPublicationCannotOverwriteSuccessor(string operation)
    {
        const string workerId = "delayed-dashboard-publication";
        var changes = new List<bool>();
        var hub = new TermHub(new TermHubConfig
        {
            OnHijackChanged = (_, enabled, _) => changes.Add(enabled),
        });
        var worker = new RecordingWorker();
        var original = new RecordingBrowser();
        var resumed = new RecordingBrowser();
        var successor = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        hub.Conn.RegisterBrowser(workerId, original, "admin");
        hub.Conn.RegisterBrowser(workerId, resumed, "admin");
        hub.Conn.RegisterBrowser(workerId, successor, "admin");
        Assert.True((await hub.Lease.TryAcquireWsAsync(workerId, original)).Ok);
        var state = hub.Registry.Get(workerId)!;
        OwnershipPublicationToken delayed;

        switch (operation)
        {
            case "acquire":
                delayed = OwnershipPublicationToken.DashboardHeld(
                    workerId, state.HijackOwnershipVersion, original);
                var ownershipVersion = hub.Conn.CleanupBrowser(workerId, original)!.Value;
                Assert.True(hub.Lease.TryRestoreWsOwnership(
                    workerId, resumed, ownershipVersion));
                break;
            case "disconnect":
                var disconnectVersion = state.HijackOwnershipVersion;
                Assert.Equal(
                    disconnectVersion,
                    hub.Conn.CleanupBrowser(workerId, original));
                delayed = OwnershipPublicationToken.Released(
                    workerId, disconnectVersion);
                Assert.True(hub.Lease.TryRestoreWsOwnership(
                    workerId, resumed, disconnectVersion));
                break;
            case "release":
                Assert.True((await hub.Lease.TryReleaseWsAsync(workerId, original)).Released);
                delayed = OwnershipPublicationToken.Released(
                    workerId, state.HijackOwnershipVersion);
                Assert.True((await hub.Lease.TryAcquireWsAsync(workerId, successor)).Ok);
                break;
            case "restore":
                var restoredVersion = hub.Conn.CleanupBrowser(workerId, original)!.Value;
                Assert.True(hub.Lease.TryRestoreWsOwnership(
                    workerId, resumed, restoredVersion));
                delayed = OwnershipPublicationToken.DashboardHeld(
                    workerId, state.HijackOwnershipVersion, resumed);
                Assert.True((await hub.Lease.TryReleaseWsAsync(workerId, resumed)).Released);
                break;
            default:
                throw new Xunit.Sdk.XunitException("unknown operation");
        }

        changes.Clear();
        Assert.False(hub.State.NotifyHijackChanged(delayed));
        Assert.Empty(changes);
    }

    [Fact]
    public async Task NeverHijackedDisconnectDoesNotPublishOwnershipLoss()
    {
        const string workerId = "never-hijacked-disconnect";
        var changes = new List<bool>();
        var hub = new TermHub(new TermHubConfig
        {
            OnHijackChanged = (_, enabled, _) => changes.Add(enabled),
        });
        var worker = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        hub.Conn.RegisterBrowser(workerId, browser, "viewer");

        Assert.True((await hub.Conn.ReconcileWorkerDisconnectAsync(workerId, worker)).Reconciled);

        Assert.Empty(changes);
        Assert.Equal(1, DecodeBrowserFrames(browser).Count(frame =>
            Type(frame) == "worker_disconnected"));
    }

    [Fact]
    public async Task ConcurrentValidRestInputWaitsForPriorInputAndThenSends()
    {
        var hub = new TermHub();
        var worker = new RecordingWorker();
        Assert.True(hub.Conn.RegisterWorker("rest-input-queue", worker));
        Assert.True((await hub.Lease.TryAcquireRestAsync(
            "rest-input-queue", "rest-owner", 30, "rest-input-lease", 0)).Ok);

        worker.DelayNextInput();
        var first = hub.Conn.SendRestInputAsync(
            "rest-input-queue", "rest-input-lease", "first-rest-input");
        await worker.InputAttempted.WaitAsync(TimeSpan.FromSeconds(5));

        var second = hub.Conn.SendRestInputAsync(
            "rest-input-queue", "rest-input-lease", "second-rest-input");
        var secondCompletedBeforeFirst = second.IsCompleted;
        worker.ReleaseInput();

        Assert.True((await first).Ok);
        Assert.True((await second).Ok);
        Assert.False(secondCompletedBeforeFirst);
        Assert.Equal(["first-rest-input", "second-rest-input"], worker.Inputs);
    }

    [Fact]
    public async Task ConcurrentValidOpenModeBrowserInputWaitsForPriorInputAndThenSends()
    {
        var hub = new TermHub();
        var worker = new RecordingWorker();
        var firstBrowser = new RecordingBrowser();
        var secondBrowser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker("browser-input-queue", worker));
        hub.Conn.RegisterBrowser("browser-input-queue", firstBrowser, "operator");
        hub.Conn.RegisterBrowser("browser-input-queue", secondBrowser, "admin");
        Assert.True(hub.Router.SetInputMode("browser-input-queue", InputModes.Open).Ok);

        worker.DelayNextInput();
        var server = NewUnstartedServer(hub);
        var first = server.SendBrowserInputAsync(
            "browser-input-queue", firstBrowser, "first-browser-input");
        await worker.InputAttempted.WaitAsync(TimeSpan.FromSeconds(5));

        var second = server.SendBrowserInputAsync(
            "browser-input-queue", secondBrowser, "second-browser-input");
        var secondCompletedBeforeFirst = second.IsCompleted;
        worker.ReleaseInput();

        Assert.True(await first);
        Assert.True(await second);
        Assert.False(secondCompletedBeforeFirst);
        Assert.Equal(["first-browser-input", "second-browser-input"], worker.Inputs);
    }

    [Theory]
    [InlineData("expiry")]
    [InlineData("replacement")]
    public async Task AutonomousOwnershipLossPublishesExactlyOnce(string transitionKind)
    {
        var clock = new ManualClock(100);
        clock.SetMonotonic(0);
        var changes = new List<(string WorkerId, bool Enabled, string? Owner)>();
        var hub = new TermHub(new TermHubConfig
        {
            Clock = clock,
            OnHijackChanged = (workerId, enabled, owner) => changes.Add((workerId, enabled, owner)),
        });
        var worker = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker("publish-loss", worker));
        hub.Conn.RegisterBrowser("publish-loss", browser, "viewer");
        Assert.True((await hub.Lease.TryAcquireRestAsync(
            "publish-loss", "rest-owner", 1, "publish-lease", 0)).Ok);
        browser.Clear();

        if (transitionKind == "expiry")
        {
            clock.SetMonotonic(2);
            _ = await hub.Lease.CleanupExpiredAsync("publish-loss");
        }
        else
        {
            Assert.True(await hub.Conn.RegisterWorkerAsync("publish-loss", new RecordingWorker()));
        }

        Assert.Equal(
            [
                ("publish-loss", true, "rest-owner"),
                ("publish-loss", false, (string?)null),
            ],
            changes);
        var stateFrame = Assert.Single(
            DecodeBrowserFrames(browser),
            frame => Type(frame) == "hijack_state");
        Assert.False(Bool(stateFrame, "hijacked"));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task ExpiredLeaseResumesWorkerWithoutAnotherAcquisition(bool restLease)
    {
        var clock = new GatedClock();
        var hub = new TermHub(new TermHubConfig { Clock = clock, DashboardHijackLeaseS = 1 });
        var worker = new RecordingWorker();
        hub.Conn.RegisterWorker("expiring", worker);
        if (restLease)
        {
            Assert.True((await hub.Lease.TryAcquireRestAsync(
                "expiring", "rest-owner", 1, "expiring-rest", 0)).Ok);
        }
        else
        {
            var browser = new object();
            hub.Conn.RegisterBrowser("expiring", browser, "admin");
            Assert.True((await hub.Lease.TryAcquireWsAsync("expiring", browser)).Ok);
        }

        await clock.SleepAttempted.WaitAsync(TimeSpan.FromSeconds(2));
        clock.SetMonotonic(2);
        clock.ReleaseSleep();

        await WaitUntilAsync(() => worker.Actions.Count == 2);
        var state = hub.Registry.Get("expiring")!;
        Assert.Null(state.HijackOwner);
        Assert.Null(state.HijackSession);
        Assert.Null(state.HijackPending);
        Assert.Equal(["pause", "resume"], worker.Actions);
    }

    [Fact]
    public async Task ForceReleaseDuringDelayedResumeReclaimDoesNotAdvertiseOwnerAndCompensates()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var original = await ConnectAsync(fixture);
        var oldToken = (await DrainHandshakeAsync(original))["resume_token"]!.ToString()!;
        await SendControlAsync(original, "hijack_request");
        await ReceiveUntilAsync(original, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        original.Abort();
        await WaitUntilAsync(() => fixture.Worker.Actions.SequenceEqual(["pause", "resume"]));

        using var resumedSocket = await ConnectAsync(fixture);
        await DrainHandshakeAsync(resumedSocket);
        fixture.Worker.DelayNextPause();
        await SendControlAsync(resumedSocket, "resume", oldToken);
        await fixture.Worker.PauseAttempted.WaitAsync(TimeSpan.FromSeconds(5));
        var ownerWhilePending = fixture.Hub.Registry.Get("resume-worker")!.HijackOwner;
        var released = await fixture.Hub.Conn.ForceReleaseHijackAsync("resume-worker");
        fixture.Worker.ReleasePause();
        var hello = await ReceiveUntilAsync(
            resumedSocket, frame => Type(frame) == "hello" && frame.ContainsKey("resumed"));

        await WaitUntilAsync(() => fixture.Worker.Actions.Count == 4);
        Assert.Null(ownerWhilePending);
        Assert.False(released);
        Assert.False(Bool(hello, "resumed"));
        Assert.Null(fixture.Hub.Registry.Get("resume-worker")!.HijackOwner);
        Assert.Equal(["pause", "resume", "pause", "resume"], fixture.Worker.Actions);
    }

    [Fact]
    public async Task ImmediateReconnectDoesNotBurnTokenWhileDisconnectResumeIsPending()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var original = await ConnectAsync(fixture);
        var oldToken = (await DrainHandshakeAsync(original))["resume_token"]!.ToString()!;
        await SendControlAsync(original, "hijack_request");
        await ReceiveUntilAsync(original, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        fixture.Worker.DelayNextResume();

        original.Abort();
        await fixture.Worker.ResumeAttempted.WaitAsync(TimeSpan.FromSeconds(5));
        using var resumedSocket = await ConnectAsync(fixture);
        await DrainHandshakeAsync(resumedSocket);
        await SendControlAsync(resumedSocket, "resume", oldToken);
        await WaitUntilAsync(() => ResumeTokenCount(fixture.Server) == 1);
        fixture.Worker.ReleaseResume();
        var hello = await ReceiveUntilAsync(
            resumedSocket, frame => Type(frame) == "hello" && frame.ContainsKey("resumed"));

        await WaitUntilAsync(() => fixture.Worker.Actions.Count == 3);
        Assert.True(Bool(hello, "resumed"));
        Assert.True(Bool(hello, "hijacked_by_me"));
        Assert.NotNull(fixture.Hub.Registry.Get("resume-worker")!.HijackOwner);
        Assert.Equal(["pause", "resume", "pause"], fixture.Worker.Actions);
    }

    [Theory]
    [InlineData("force")]
    [InlineData("release")]
    [InlineData("expiry")]
    public async Task CancelledLifecycleWaitClearsItsReservationWithoutCancelingAuthorizedInput(
        string operation)
    {
        var clock = new ManualClock(100);
        clock.SetMonotonic(0);
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        var worker = new RecordingWorker();
        Assert.True(hub.Conn.RegisterWorker("cancelled-transition", worker));
        Assert.True((await hub.Lease.TryAcquireRestAsync(
            "cancelled-transition", "owner", 10, "lease", 0)).Ok);
        worker.DelayNextInput();
        var input = hub.Lease.SendRestInputAsync("cancelled-transition", "lease", "echo safe");
        await worker.InputAttempted.WaitAsync(TimeSpan.FromSeconds(1));
        if (operation == "expiry") clock.SetMonotonic(11);
        using var cancellation = new CancellationTokenSource();
        var transition = operation switch
        {
            "force" => WaitForForceAsync(),
            "release" => WaitForReleaseAsync(),
            _ => WaitForExpiryAsync(),
        };
        await WaitUntilAsync(() =>
            hub.Registry.Get("cancelled-transition")!.HijackPending is not null);

        cancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(async () => await transition);
        worker.ReleaseInput();

        Assert.True((await input).Ok);
        var state = hub.Registry.Get("cancelled-transition")!;
        Assert.Null(state.HijackPending);
        Assert.Null(state.DisconnectResumeCompletion);

        async Task WaitForForceAsync() =>
            _ = await hub.Lease.ForceReleaseAsync("cancelled-transition", cancellation.Token);

        async Task WaitForReleaseAsync() =>
            _ = await hub.Lease.ReleaseRestAsync("cancelled-transition", "lease", cancellation.Token);

        async Task WaitForExpiryAsync() =>
            _ = await hub.Lease.CleanupExpiredAsync("cancelled-transition", cancellation.Token);
    }

    [Fact]
    public async Task ReplacementSurvivesResumeAndOwnershipPublicationFailures()
    {
        var hub = new TermHub(new TermHubConfig
        {
            OnHijackChanged = (_, enabled, _) =>
            {
                if (!enabled) throw new InvalidOperationException("publication failed");
            },
        });
        var predecessor = new ThrowingResumeWorker();
        var replacement = new RecordingWorker();
        Assert.True(hub.Conn.RegisterWorker("replacement-failures", predecessor));
        Assert.True((await hub.Lease.TryAcquireRestAsync(
            "replacement-failures", "owner", 30, "lease", 0)).Ok);

        Assert.True(await hub.Conn.RegisterWorkerAsync("replacement-failures", replacement));

        var state = hub.Registry.Get("replacement-failures")!;
        Assert.Same(replacement, state.WorkerWs);
        Assert.Null(state.HijackSession);
        Assert.True(predecessor.Aborted);
    }

    [Fact]
    public async Task WorkerTeardownSurvivesOfflineAndOwnershipCallbackFailures()
    {
        var hub = new TermHub(new TermHubConfig
        {
            OnHijackChanged = (_, enabled, _) =>
            {
                if (!enabled) throw new InvalidOperationException("publication failed");
            },
        });
        hub.Conn.ConfigureWorkerOfflineMarker(_ =>
            throw new InvalidOperationException("offline marker failed"));
        var worker = new RecordingWorker();
        Assert.True(hub.Conn.RegisterWorker("teardown-failures", worker));
        Assert.True((await hub.Lease.TryAcquireRestAsync(
            "teardown-failures", "owner", 30, "lease", 0)).Ok);

        var result = await hub.Conn.ReconcileWorkerDisconnectAsync("teardown-failures", worker);

        Assert.True(result.Reconciled);
        Assert.True(result.WasHijacked);
        Assert.Null(hub.Registry.Get("teardown-failures")!.WorkerWs);
    }

    [Fact]
    public async Task DashboardCleanupSurvivesOwnershipPublicationFailure()
    {
        var hub = new TermHub(new TermHubConfig
        {
            OnHijackChanged = (_, enabled, _) =>
            {
                if (!enabled) throw new InvalidOperationException("publication failed");
            },
        });
        var worker = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker("cleanup-publication", worker));
        hub.Conn.RegisterBrowser("cleanup-publication", browser, "admin");
        Assert.True(hub.Lease.TryAcquireWs("cleanup-publication", browser).Ok);

        var version = hub.Conn.CleanupBrowser("cleanup-publication", browser);

        Assert.NotNull(version);
        Assert.Null(hub.Registry.Get("cleanup-publication")!.HijackOwner);
    }

    [Fact]
    public async Task FailedDisconnectResumeContainsAbortFailureAndReconcilesWorker()
    {
        var hub = new TermHub();
        var worker = new ThrowingResumeAndAbortWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker("abort-failure", worker));
        hub.Conn.RegisterBrowser("abort-failure", browser, "admin");
        Assert.True(hub.Lease.TryAcquireWs("abort-failure", browser).Ok);
        var version = Assert.IsType<long>(hub.Conn.CleanupBrowser("abort-failure", browser));

        var resumed = await hub.Conn.ResumeWorkerIfOwnershipUnchangedAsync(
            "abort-failure",
            version,
            HijackLeaseManager.ResumeFrame("disconnect", 1));

        Assert.False(resumed.Resumed);
        Assert.IsType<IOException>(resumed.Error);
        Assert.Null(hub.Registry.Get("abort-failure")!.WorkerWs);
    }

    [Fact]
    public async Task WorkerThatClosesDuringSendCannotReportSuccessfulDelivery()
    {
        var hub = new TermHub();
        var worker = new DeactivateDuringSendWorker();
        Assert.True(hub.Conn.RegisterWorker("close-during-send", worker));
        Assert.True(hub.Conn.SetWorkerHello("close-during-send", worker, InputModes.Hijack, 1));

        var result = await hub.Conn.SendWorkerAsync(
            "close-during-send", new Dictionary<string, object?> { ["type"] = "input" });

        Assert.False(result.Ok);
        Assert.Null(result.Error);
    }

    [Fact]
    public async Task FailedPauseContainsAbortFailureAndFencesWorkerOffline()
    {
        var hub = new TermHub();
        var worker = new ThrowingPauseAndAbortWorker();
        Assert.True(hub.Conn.RegisterWorker("pause-abort-failure", worker));

        var result = await hub.Lease.TryAcquireRestAsync(
            "pause-abort-failure", "owner", 30, "lease", 0);

        Assert.False(result.Ok);
        Assert.Equal("no_worker", result.Reason);
        Assert.Null(hub.Registry.Get("pause-abort-failure")!.WorkerWs);
    }

    [Fact]
    public async Task FailedPauseRepairsCapturedWorkerWithoutDisconnectingReplacement()
    {
        var hub = new TermHub();
        var predecessor = new RecordingWorker();
        var replacement = new RecordingWorker();
        Assert.True(hub.Conn.RegisterWorker("pause-repair", predecessor));
        var pause = predecessor.DelayNextPause(fail: true);
        var acquire = hub.Lease.TryAcquireRestAsync(
            "pause-repair", "owner", 30, "lease", 0);
        await pause.Attempted.WaitAsync(TimeSpan.FromSeconds(1));
        var state = hub.Registry.Get("pause-repair")!;
        state.PendingPauseObligation = state.HijackPending;
        state.WorkerWs = replacement;

        pause.Release();
        var result = await acquire;

        Assert.False(result.Ok);
        Assert.Same(replacement, state.WorkerWs);
        Assert.Null(state.PendingPauseObligation);
        Assert.Equal(["resume"], predecessor.Actions);
    }

    [Fact]
    public async Task ExpiryArmFailureDoesNotUndoSuccessfulAcquisition()
    {
        var clock = new ThrowingSleepClock();
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        var worker = new RecordingWorker();
        Assert.True(hub.Conn.RegisterWorker("arm-failure", worker));

        var acquired = await hub.Lease.TryAcquireRestAsync(
            "arm-failure", "owner", 30, "lease", 0);
        await clock.SleepAttempted.Task.WaitAsync(TimeSpan.FromSeconds(1));

        Assert.True(acquired.Ok);
        Assert.NotNull(hub.Registry.Get("arm-failure")!.HijackSession);
    }
}
