//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Tests.Server;

public sealed partial class ResumeLifecycleIntegrationTests
{
    internal sealed record ResumeContractEvidence(
        bool ResumeSucceeded,
        bool OwnershipRestored,
        bool ReplayRejected,
        bool CompetingOwnerPreserved);

    internal static async Task<ResumeContractEvidence> RunCurrentOwnerContractScenarioAsync()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var original = await ConnectAsync(fixture);
        var oldToken = (await DrainHandshakeAsync(original))["resume_token"]!.ToString()!;
        await SendControlAsync(original, "hijack_request");
        await ReceiveUntilAsync(original, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        original.Abort();
        await WaitUntilAsync(() => fixture.Hub.Registry.Get("resume-worker")!.Browsers.Count == 0);
        await WaitUntilAsync(() => fixture.Worker.Actions.SequenceEqual(["pause", "resume"]));

        using var resumed = await ConnectAsync(fixture);
        await DrainHandshakeAsync(resumed);
        await SendControlAsync(resumed, "resume", oldToken);
        var restored = await ReceiveUntilAsync(
            resumed, frame => Type(frame) == "hello" && frame.ContainsKey("resumed"));
        Assert.True(Bool(restored, "resumed"));
        Assert.True(Bool(restored, "hijacked_by_me"));
        var restoredOwner = fixture.Hub.Registry.Get("resume-worker")!.HijackOwner;

        await SendControlAsync(resumed, "resume", oldToken);
        await SendControlAsync(resumed, "ping");
        var replayFrames = await ReceiveThroughAsync(resumed, frame => Type(frame) == "pong");
        Assert.DoesNotContain(
            replayFrames,
            frame => Type(frame) == "hello" && Bool(frame, "resumed"));
        Assert.Same(restoredOwner, fixture.Hub.Registry.Get("resume-worker")!.HijackOwner);
        return new(true, true, true, false);
    }

    internal static async Task<ResumeContractEvidence> RunCompetingOwnerContractScenarioAsync()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var original = await ConnectAsync(fixture);
        var oldToken = (await DrainHandshakeAsync(original))["resume_token"]!.ToString()!;
        await SendControlAsync(original, "hijack_request");
        await ReceiveUntilAsync(original, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        original.Abort();
        await WaitUntilAsync(() => fixture.Hub.Registry.Get("resume-worker")!.Browsers.Count == 0);

        using var competitor = await ConnectAsync(fixture);
        await DrainHandshakeAsync(competitor);
        await SendControlAsync(competitor, "hijack_request");
        await ReceiveUntilAsync(competitor, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        var competingOwner = fixture.Hub.Registry.Get("resume-worker")!.HijackOwner;

        using var attempted = await ConnectAsync(fixture);
        await DrainHandshakeAsync(attempted);
        await SendControlAsync(attempted, "resume", oldToken);
        var rejected = await ReceiveUntilAsync(
            attempted, frame => Type(frame) == "hello" && frame.ContainsKey("resumed"));
        Assert.False(Bool(rejected, "resumed"));
        Assert.Same(competingOwner, fixture.Hub.Registry.Get("resume-worker")!.HijackOwner);
        await SendControlAsync(competitor, "heartbeat");
        await ReceiveUntilAsync(competitor, frame => Type(frame) == "heartbeat_ack");
        return new(false, false, false, true);
    }

    internal static async Task<bool> RunNonOwnerStepContractScenarioAsync()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var owner = await ConnectAsync(fixture);
        using var nonOwner = await ConnectAsync(fixture);
        await DrainHandshakeAsync(owner);
        await DrainHandshakeAsync(nonOwner);
        await SendControlAsync(owner, "hijack_request");
        await ReceiveUntilAsync(owner, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        var before = fixture.Worker.Actions.Count;
        await SendControlAsync(nonOwner, "hijack_step");
        await SendControlAsync(nonOwner, "ping");
        await ReceiveUntilAsync(nonOwner, frame => Type(frame) == "pong");
        Assert.Equal(before, fixture.Worker.Actions.Count);
        await SendControlAsync(owner, "heartbeat");
        await ReceiveUntilAsync(owner, frame => Type(frame) == "heartbeat_ack");
        return true;
    }

    [Fact]
    public async Task ProductionDashboardAcquireReleasePublishesExactlyOnce()
    {
        var changes = new List<(bool Enabled, string? Owner)>();
        var fixture = await BootAsync(
            (enabled, owner) => changes.Add((enabled, owner)));
        await using var server = fixture.Server;
        using var browser = await ConnectAsync(fixture);
        await DrainHandshakeAsync(browser);

        await SendControlAsync(browser, "hijack_request");
        var acquired = await ReceiveUntilAsync(
            browser,
            frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        await SendControlAsync(browser, "hijack_release");
        var released = await ReceiveUntilAsync(
            browser,
            frame => Type(frame) == "hijack_state" && !Bool(frame, "hijacked"));

        Assert.Equal("me", acquired["owner"]?.ToString());
        Assert.False(Bool(released, "hijacked"));
        Assert.Equal(
            [
                (true, "dashboard"),
                (false, (string?)null),
            ],
            changes);
    }

    [Fact]
    public async Task ProductionDashboardResumePublishesRestoredOwnershipExactlyOnce()
    {
        var changes = new List<(bool Enabled, string? Owner)>();
        var fixture = await BootAsync(
            (enabled, owner) => changes.Add((enabled, owner)));
        await using var server = fixture.Server;
        using var original = await ConnectAsync(fixture);
        var oldToken = (await DrainHandshakeAsync(original))["resume_token"]!.ToString()!;
        await SendControlAsync(original, "hijack_request");
        await ReceiveUntilAsync(
            original,
            frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));

        original.Abort();
        await WaitUntilAsync(() => fixture.Hub.Registry.Get("resume-worker")!.Browsers.Count == 0);
        await WaitUntilAsync(() => fixture.Worker.Actions.SequenceEqual(["pause", "resume"]));

        using var resumedSocket = await ConnectAsync(fixture);
        await DrainHandshakeAsync(resumedSocket);
        await SendControlAsync(resumedSocket, "resume", oldToken);
        var resumedHello = await ReceiveUntilAsync(
            resumedSocket,
            frame => Type(frame) == "hello" && frame.ContainsKey("resumed"));
        var restored = await ReceiveUntilAsync(
            resumedSocket,
            frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));

        Assert.True(Bool(resumedHello, "resumed"));
        Assert.Equal("me", restored["owner"]?.ToString());
        Assert.Equal(
            [
                (true, "dashboard"),
                (false, (string?)null),
                (true, "dashboard"),
            ],
            changes);
    }

    [Fact]
    public async Task CurrentOwnerTokenRestoresHijackAndReportsResumedTrue()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var original = await ConnectAsync(fixture);
        var oldToken = (await DrainHandshakeAsync(original))["resume_token"]!.ToString()!;
        await SendControlAsync(original, "hijack_request");
        await ReceiveUntilAsync(original, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        await WaitUntilAsync(() => fixture.Hub.Registry.Get("resume-worker")!.HijackOwner is not null);

        original.Abort();
        await WaitUntilAsync(() => fixture.Hub.Registry.Get("resume-worker")!.Browsers.Count == 0);
        await WaitUntilAsync(() => fixture.Worker.Actions.SequenceEqual(["pause", "resume"]));

        using var resumedSocket = await ConnectAsync(fixture);
        await DrainHandshakeAsync(resumedSocket);
        await SendControlAsync(resumedSocket, "resume", oldToken);
        var resumedHello = await ReceiveUntilAsync(
            resumedSocket, frame => Type(frame) == "hello" && frame.ContainsKey("resumed"));

        Assert.True(Bool(resumedHello, "resumed"));
        Assert.True(Bool(resumedHello, "hijacked_by_me"));
        Assert.NotEqual(oldToken, resumedHello["resume_token"]?.ToString());
        Assert.NotNull(fixture.Hub.Registry.Get("resume-worker")!.HijackOwner);
        await WaitUntilAsync(() => fixture.Worker.Actions.Count == 3);
        Assert.Equal(["pause", "resume", "pause"], fixture.Worker.Actions);
    }

    [Fact]
    public async Task BrowserHijackStepRequiresCurrentDashboardOwner()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var owner = await ConnectAsync(fixture);
        using var nonOwner = await ConnectAsync(fixture);
        await DrainHandshakeAsync(owner);
        await DrainHandshakeAsync(nonOwner);

        await SendControlAsync(owner, "hijack_request");
        await ReceiveUntilAsync(owner, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        await WaitUntilAsync(() => fixture.Hub.Registry.Get("resume-worker")!.HijackOwner is not null);
        var before = fixture.Worker.Actions.Count;

        await SendControlAsync(nonOwner, "hijack_step");
        await SendControlAsync(nonOwner, "ping");
        await ReceiveUntilAsync(nonOwner, frame => Type(frame) == "pong");
        Assert.Equal(before, fixture.Worker.Actions.Count);

        await SendControlAsync(owner, "hijack_step");
        await SendControlAsync(owner, "ping");
        await ReceiveUntilAsync(owner, frame => Type(frame) == "pong");
        await WaitUntilAsync(() => fixture.Worker.Actions.Count == before + 1);
        Assert.Equal("step", fixture.Worker.Actions[^1]);
    }

    [Fact]
    public async Task LaterOwnerMakesOldTokenTruthfullyReportResumedFalse()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var original = await ConnectAsync(fixture);
        var oldToken = (await DrainHandshakeAsync(original))["resume_token"]!.ToString()!;
        await SendControlAsync(original, "hijack_request");
        await ReceiveUntilAsync(original, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        original.Abort();
        await WaitUntilAsync(() => fixture.Hub.Registry.Get("resume-worker")!.Browsers.Count == 0);
        await Task.Delay(50);

        using var later = await ConnectAsync(fixture);
        await DrainHandshakeAsync(later);
        await SendControlAsync(later, "hijack_request");
        await ReceiveUntilAsync(later, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        await SendControlAsync(later, "hijack_release");
        await ReceiveUntilAsync(later, frame => Type(frame) == "hijack_state" && !Bool(frame, "hijacked"));

        using var attempted = await ConnectAsync(fixture);
        await DrainHandshakeAsync(attempted);
        await SendControlAsync(attempted, "resume", oldToken);
        var hello = await ReceiveUntilAsync(
            attempted, frame => Type(frame) == "hello" && frame.ContainsKey("resumed"));

        Assert.False(Bool(hello, "resumed"));
        Assert.False(Bool(hello, "hijacked_by_me"));
        Assert.Null(fixture.Hub.Registry.Get("resume-worker")!.HijackOwner);
    }

    [Fact]
    public async Task ConsumedNonOwnerTokenReportsFalseWithFreshUsableToken()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var socket = await ConnectAsync(fixture);
        var oldToken = (await DrainHandshakeAsync(socket))["resume_token"]!.ToString()!;

        await SendControlAsync(socket, "resume", oldToken);
        var hello = await ReceiveUntilAsync(
            socket, frame => Type(frame) == "hello" && frame.ContainsKey("resumed"));

        Assert.False(Bool(hello, "resumed"));
        Assert.NotEqual(oldToken, hello["resume_token"]?.ToString());
    }

    [Fact]
    public async Task DisconnectedNonOwnerTokenResumesOnNewSocketWithoutPausingWorker()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var original = await ConnectAsync(fixture);
        var oldToken = (await DrainHandshakeAsync(original))["resume_token"]!.ToString()!;

        original.Abort();
        await WaitUntilAsync(() => fixture.Hub.Registry.Get("resume-worker")!.Browsers.Count == 0);
        Assert.Empty(fixture.Worker.Actions);

        using var resumedSocket = await ConnectAsync(fixture);
        await DrainHandshakeAsync(resumedSocket);
        await SendControlAsync(resumedSocket, "resume", oldToken);
        var hello = await ReceiveUntilAsync(
            resumedSocket, frame => Type(frame) == "hello" && frame.ContainsKey("resumed"));

        Assert.True(Bool(hello, "resumed"));
        Assert.False(Bool(hello, "hijacked_by_me"));
        Assert.Empty(fixture.Worker.Actions);
    }

    [Fact]
    public async Task RejectedDashboardRequestDuringDisconnectResumeDoesNotPauseWorker()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var original = await ConnectAsync(fixture);
        await DrainHandshakeAsync(original);
        await SendControlAsync(original, "hijack_request");
        await ReceiveUntilAsync(original, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        using var contender = await ConnectAsync(fixture);
        await DrainHandshakeAsync(contender);
        var requestRejected = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        fixture.Worker.AfterResume = async () =>
        {
            await SendControlAsync(contender, "hijack_request");
            await ReceiveUntilAsync(contender, frame => Type(frame) == "error");
            requestRejected.TrySetResult();
        };

        original.Abort();
        await requestRejected.Task.WaitAsync(TimeSpan.FromSeconds(5));

        Assert.Null(fixture.Hub.Registry.Get("resume-worker")!.HijackOwner);
        Assert.Equal(["pause", "resume"], fixture.Worker.Actions);
    }

    [Fact]
    public async Task FailedFreshDashboardPauseDoesNotPublishOwnership()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var browser = await ConnectAsync(fixture);
        await DrainHandshakeAsync(browser);
        fixture.Worker.FailNextPause();

        await SendControlAsync(browser, "hijack_request");
        await fixture.Worker.PauseAttempted.WaitAsync(TimeSpan.FromSeconds(5));
        var error = await ReceiveUntilAsync(browser, frame => Type(frame) == "error");

        Assert.Equal("Hijack failed: no_worker", error["message"]?.ToString());
        Assert.Null(fixture.Hub.Registry.Get("resume-worker")!.HijackOwner);
        Assert.Equal(["resume"], fixture.Worker.Actions);
    }

    [Fact]
    public async Task ForceReleaseDuringDelayedFreshPauseEndsResumedWithoutOwner()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var browser = await ConnectAsync(fixture);
        await DrainHandshakeAsync(browser);
        fixture.Worker.DelayNextPause();

        await SendControlAsync(browser, "hijack_request");
        await fixture.Worker.PauseAttempted.WaitAsync(TimeSpan.FromSeconds(5));
        var released = await fixture.Hub.Conn.ForceReleaseHijackAsync("resume-worker");
        fixture.Worker.ReleasePause();

        await WaitUntilAsync(() => fixture.Worker.Actions.Count == 2);
        Assert.False(released);
        Assert.Null(fixture.Hub.Registry.Get("resume-worker")!.HijackOwner);
        Assert.Equal(["pause", "resume"], fixture.Worker.Actions);
    }

    [Fact]
    public async Task CleanupDuringDelayedFreshPauseEndsResumedWithoutOwner()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var browser = await ConnectAsync(fixture);
        await DrainHandshakeAsync(browser);
        fixture.Worker.DelayNextPause();

        await SendControlAsync(browser, "hijack_request");
        await fixture.Worker.PauseAttempted.WaitAsync(TimeSpan.FromSeconds(5));
        var browserConnection = Assert.Single(
            fixture.Hub.Registry.Get("resume-worker")!.Browsers.Keys);
        fixture.Hub.Conn.CleanupBrowser("resume-worker", browserConnection);
        fixture.Worker.ReleasePause();

        await WaitUntilAsync(() => fixture.Worker.Actions.Count == 2);
        Assert.Null(fixture.Hub.Registry.Get("resume-worker")!.HijackOwner);
        Assert.Equal(["pause", "resume"], fixture.Worker.Actions);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task FailedOrCanceledSuccessorDischargesCanceledPredecessorPauseObligation(
        bool cancelSuccessor)
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var predecessor = await ConnectAsync(fixture);
        await DrainHandshakeAsync(predecessor);
        using var successor = await ConnectAsync(fixture);
        await DrainHandshakeAsync(successor);
        var predecessorPause = fixture.Worker.DelayNextPause();

        await SendControlAsync(predecessor, "hijack_request");
        await predecessorPause.Attempted.WaitAsync(TimeSpan.FromSeconds(5));
        var state = fixture.Hub.Registry.Get("resume-worker")!;
        fixture.Hub.Conn.CleanupBrowser("resume-worker", state.PendingDashboardBrowser!);
        var successorPause = fixture.Worker.DelayNextPause(
            fail: !cancelSuccessor,
            cancel: cancelSuccessor);
        await SendControlAsync(successor, "hijack_request");
        await successorPause.Attempted.WaitAsync(TimeSpan.FromSeconds(5));

        predecessorPause.Release();
        await WaitUntilAsync(() => fixture.Worker.Actions.SequenceEqual(["pause"]));
        await ReceiveUntilAsync(predecessor, frame => Type(frame) == "error");
        successorPause.Release();
        if (!cancelSuccessor)
        {
            await ReceiveUntilAsync(successor, frame => Type(frame) == "error");
        }

        await WaitUntilAsync(() => fixture.Worker.Actions.Count == 2);
        Assert.Null(state.HijackOwner);
        Assert.Null(state.PendingPauseObligation);
        Assert.Equal(["pause", "resume"], fixture.Worker.Actions);
    }

    [Fact]
    public async Task SuccessfulSuccessorCommitDischargesCanceledPredecessorPauseObligation()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var predecessor = await ConnectAsync(fixture);
        await DrainHandshakeAsync(predecessor);
        using var successor = await ConnectAsync(fixture);
        await DrainHandshakeAsync(successor);
        var predecessorPause = fixture.Worker.DelayNextPause();

        await SendControlAsync(predecessor, "hijack_request");
        await predecessorPause.Attempted.WaitAsync(TimeSpan.FromSeconds(5));
        var state = fixture.Hub.Registry.Get("resume-worker")!;
        fixture.Hub.Conn.CleanupBrowser("resume-worker", state.PendingDashboardBrowser!);
        var successorPause = fixture.Worker.DelayNextPause();
        await SendControlAsync(successor, "hijack_request");
        await successorPause.Attempted.WaitAsync(TimeSpan.FromSeconds(5));

        predecessorPause.Release();
        await ReceiveUntilAsync(predecessor, frame => Type(frame) == "error");
        Assert.NotNull(state.PendingPauseObligation);
        Assert.Equal(state.HijackPending, state.PendingPauseObligation);
        successorPause.Release();
        await ReceiveUntilAsync(
            successor, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));

        Assert.NotNull(state.HijackOwner);
        Assert.Null(state.PendingPauseObligation);
        Assert.Equal(["pause", "pause"], fixture.Worker.Actions);
    }

    [Fact]
    public async Task NotDeliveredDashboardSuccessorDischargesInheritedPauseObligation()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var predecessor = await ConnectAsync(fixture);
        await DrainHandshakeAsync(predecessor);
        using var successor = await ConnectAsync(fixture);
        await DrainHandshakeAsync(successor);
        var predecessorPause = fixture.Worker.DelayNextPause();

        await SendControlAsync(predecessor, "hijack_request");
        await predecessorPause.Attempted.WaitAsync(TimeSpan.FromSeconds(5));
        var state = fixture.Hub.Registry.Get("resume-worker")!;
        fixture.Hub.Conn.CleanupBrowser("resume-worker", state.PendingDashboardBrowser!);
        var inactiveCheck = fixture.Worker.BlockNextActiveCheck();
        await SendControlAsync(successor, "hijack_request");
        await inactiveCheck.Attempted.WaitAsync(TimeSpan.FromSeconds(5));

        predecessorPause.Release();
        await ReceiveUntilAsync(predecessor, frame => Type(frame) == "error");
        try
        {
            await WaitUntilAsync(() => state.PendingPauseObligation == state.HijackPending);
            fixture.Worker.Deactivate();
        }
        finally
        {
            inactiveCheck.Release();
        }
        await ReceiveUntilAsync(successor, frame => Type(frame) == "error");

        await WaitUntilAsync(() => fixture.Worker.Actions.Count == 2);
        Assert.Null(state.HijackOwner);
        Assert.Null(state.PendingPauseObligation);
        Assert.Equal(["pause", "resume"], fixture.Worker.Actions);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task FailedOrCanceledRestSuccessorDischargesDashboardPauseObligation(
        bool cancelSuccessor)
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var predecessor = await ConnectAsync(fixture);
        await DrainHandshakeAsync(predecessor);
        var predecessorPause = fixture.Worker.DelayNextPause();

        await SendControlAsync(predecessor, "hijack_request");
        await predecessorPause.Attempted.WaitAsync(TimeSpan.FromSeconds(5));
        var state = fixture.Hub.Registry.Get("resume-worker")!;
        fixture.Hub.Conn.CleanupBrowser("resume-worker", state.PendingDashboardBrowser!);
        var successorPause = fixture.Worker.DelayNextPause(
            fail: !cancelSuccessor,
            cancel: cancelSuccessor);
        var restAcquire = fixture.Hub.Lease.TryAcquireRestAsync(
            "resume-worker", "rest-owner", 30, "rest-successor", 10);
        await successorPause.Attempted.WaitAsync(TimeSpan.FromSeconds(5));

        predecessorPause.Release();
        await ReceiveUntilAsync(predecessor, frame => Type(frame) == "error");
        successorPause.Release();
        if (cancelSuccessor)
        {
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => restAcquire);
        }
        else
        {
            Assert.False((await restAcquire).Ok);
        }

        await WaitUntilAsync(() => fixture.Worker.Actions.Count == 2);
        Assert.Null(state.HijackOwner);
        Assert.Null(state.HijackSession);
        Assert.Null(state.PendingPauseObligation);
        Assert.Equal(["pause", "resume"], fixture.Worker.Actions);
    }

    [Fact]
    public async Task SuccessfulRestSuccessorCommitDischargesDashboardPauseObligation()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var predecessor = await ConnectAsync(fixture);
        await DrainHandshakeAsync(predecessor);
        var predecessorPause = fixture.Worker.DelayNextPause();

        await SendControlAsync(predecessor, "hijack_request");
        await predecessorPause.Attempted.WaitAsync(TimeSpan.FromSeconds(5));
        var state = fixture.Hub.Registry.Get("resume-worker")!;
        fixture.Hub.Conn.CleanupBrowser("resume-worker", state.PendingDashboardBrowser!);
        var successorPause = fixture.Worker.DelayNextPause();
        var restAcquire = fixture.Hub.Lease.TryAcquireRestAsync(
            "resume-worker", "rest-owner", 30, "rest-successor", 10);
        await successorPause.Attempted.WaitAsync(TimeSpan.FromSeconds(5));

        predecessorPause.Release();
        await ReceiveUntilAsync(predecessor, frame => Type(frame) == "error");
        Assert.NotNull(state.PendingPauseObligation);
        Assert.Equal(state.HijackPending, state.PendingPauseObligation);
        successorPause.Release();
        var acquired = await restAcquire;

        Assert.True(acquired.Ok);
        Assert.NotNull(state.HijackSession);
        Assert.Null(state.PendingPauseObligation);
        Assert.Equal(["pause", "pause"], fixture.Worker.Actions);
    }

    [Fact]
    public async Task NotDeliveredRestSuccessorDischargesInheritedPauseObligation()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var predecessor = await ConnectAsync(fixture);
        await DrainHandshakeAsync(predecessor);
        var predecessorPause = fixture.Worker.DelayNextPause();

        await SendControlAsync(predecessor, "hijack_request");
        await predecessorPause.Attempted.WaitAsync(TimeSpan.FromSeconds(5));
        var state = fixture.Hub.Registry.Get("resume-worker")!;
        fixture.Hub.Conn.CleanupBrowser("resume-worker", state.PendingDashboardBrowser!);
        var inactiveCheck = fixture.Worker.BlockNextActiveCheck();
        var restAcquire = Task.Run(async () => await fixture.Hub.Lease.TryAcquireRestAsync(
            "resume-worker", "rest-owner", 30, "rest-successor", 10));
        await inactiveCheck.Attempted.WaitAsync(TimeSpan.FromSeconds(5));

        predecessorPause.Release();
        await ReceiveUntilAsync(predecessor, frame => Type(frame) == "error");
        try
        {
            await WaitUntilAsync(() => state.PendingPauseObligation == state.HijackPending);
            fixture.Worker.Deactivate();
        }
        finally
        {
            inactiveCheck.Release();
        }
        Assert.False((await restAcquire).Ok);

        await WaitUntilAsync(() => fixture.Worker.Actions.Count == 2);
        Assert.Null(state.HijackOwner);
        Assert.Null(state.HijackSession);
        Assert.Null(state.PendingPauseObligation);
        Assert.Equal(["pause", "resume"], fixture.Worker.Actions);
    }

    [Fact]
    public async Task WorkerReplacementWaitsForRestAcquirePauseFence()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        var pause = fixture.Worker.DelayNextPause();
        var acquire = fixture.Hub.Lease.TryAcquireRestAsync(
            "resume-worker", "old-owner", 30, "replace-worker", 10);
        await pause.Attempted.WaitAsync(TimeSpan.FromSeconds(5));
        var replacement = new RecordingWorker();
        var replacementTransition = fixture.Hub.Conn.RegisterWorkerAsync(
            "resume-worker", replacement);
        Assert.False(replacementTransition.IsCompleted);

        pause.Release();
        var result = await acquire;
        Assert.True(await replacementTransition.WaitAsync(TimeSpan.FromSeconds(1)));

        await WaitUntilAsync(() => fixture.Worker.Actions.Count == 2);
        Assert.True(result.Ok);
        Assert.Null(fixture.Hub.Registry.Get("resume-worker")!.HijackSession);
        Assert.Same(replacement, fixture.Hub.Registry.Get("resume-worker")!.WorkerWs);
        Assert.Equal(["pause", "resume"], fixture.Worker.Actions);
        Assert.Empty(replacement.Actions);
    }
}
