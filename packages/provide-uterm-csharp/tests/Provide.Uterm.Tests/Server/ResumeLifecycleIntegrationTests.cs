//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Reflection;
using System.Text;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests.Server;

public sealed class ResumeLifecycleIntegrationTests
{
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
    public async Task RestAcquireDoesNotCommitAfterWorkerReplacement()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        var pause = fixture.Worker.DelayNextPause();
        var acquire = fixture.Hub.Lease.TryAcquireRestAsync(
            "resume-worker", "old-owner", 30, "replace-worker", 10);
        await pause.Attempted.WaitAsync(TimeSpan.FromSeconds(5));
        var replacement = new RecordingWorker();
        fixture.Hub.Conn.RegisterWorker("resume-worker", replacement);

        pause.Release();
        var result = await acquire;

        await WaitUntilAsync(() => fixture.Worker.Actions.Count == 2);
        Assert.False(result.Ok);
        Assert.Null(fixture.Hub.Registry.Get("resume-worker")!.HijackSession);
        Assert.Equal(["pause", "resume"], fixture.Worker.Actions);
        Assert.Empty(replacement.Actions);
    }

    [Fact]
    public async Task RestAcquireInternalReservationPreventsHijackIdAbaCommit()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        var predecessorPause = fixture.Worker.DelayNextPause();
        var predecessor = fixture.Hub.Lease.TryAcquireRestAsync(
            "resume-worker", "predecessor", 30, "shared-id", 10);
        await predecessorPause.Attempted.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.False(await fixture.Hub.Conn.ForceReleaseHijackAsync("resume-worker"));
        var successorPause = fixture.Worker.DelayNextPause();
        var successor = fixture.Hub.Lease.TryAcquireRestAsync(
            "resume-worker", "successor", 30, "shared-id", 20);
        await successorPause.Attempted.WaitAsync(TimeSpan.FromSeconds(5));

        predecessorPause.Release();
        Assert.False((await predecessor).Ok);
        successorPause.Release();
        Assert.True((await successor).Ok);

        var session = fixture.Hub.Registry.Get("resume-worker")!.HijackSession;
        Assert.NotNull(session);
        Assert.Equal("successor", session.Owner);
        Assert.Equal(["pause", "pause"], fixture.Worker.Actions);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task ClosedWorkerFailsPauseAcquisitionWithoutPublishingOwner(bool restAcquire)
    {
        var hub = new TermHub(new TermHubConfig());
        var worker = new ClosedWorker();
        hub.Conn.RegisterWorker("closed", worker);
        var (sent, _) = await hub.Conn.SendWorkerAsync(
            "closed", new Dictionary<string, object?> { ["type"] = "control", ["action"] = "pause" });
        Assert.False(sent);

        bool acquired;
        if (restAcquire)
        {
            (acquired, _) = await hub.Lease.TryAcquireRestAsync(
                "closed", "rest-owner", 30, "closed-rest", 10);
        }
        else
        {
            var browser = new object();
            hub.Conn.RegisterBrowser("closed", browser, "admin");
            (acquired, _) = await hub.Lease.TryAcquireWsAsync("closed", browser);
        }

        Assert.False(acquired);
        Assert.Null(hub.Registry.Get("closed")!.HijackOwner);
        Assert.Null(hub.Registry.Get("closed")!.HijackSession);
        Assert.Equal(0, worker.SendCount);
    }

    [Theory]
    [InlineData(false, false)]
    [InlineData(false, true)]
    [InlineData(true, false)]
    [InlineData(true, true)]
    public async Task PossiblyDeliveredPauseIsCompensatedAfterThrowOrCancellation(
        bool restAcquire,
        bool cancelSend)
    {
        var hub = new TermHub(new TermHubConfig());
        var worker = new RecordingWorker();
        worker.ThrowAfterNextPause(cancelSend);
        hub.Conn.RegisterWorker("uncertain", worker);

        if (restAcquire)
        {
            var acquire = hub.Lease.TryAcquireRestAsync(
                "uncertain", "rest-owner", 30, "uncertain-rest", 10);
            if (cancelSend) await Assert.ThrowsAnyAsync<OperationCanceledException>(() => acquire);
            else Assert.False((await acquire).Ok);
        }
        else
        {
            var browser = new object();
            hub.Conn.RegisterBrowser("uncertain", browser, "admin");
            var acquire = hub.Lease.TryAcquireWsAsync("uncertain", browser);
            if (cancelSend) await Assert.ThrowsAnyAsync<OperationCanceledException>(() => acquire);
            else Assert.False((await acquire).Ok);
        }

        Assert.Null(hub.Registry.Get("uncertain")!.HijackOwner);
        Assert.Null(hub.Registry.Get("uncertain")!.HijackSession);
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

    private static async Task<Dictionary<string, object?>> DrainHandshakeAsync(ClientWebSocket socket)
    {
        Dictionary<string, object?>? hello = null;
        for (var i = 0; i < 3; i++)
        {
            var frame = await ReceiveFrameAsync(socket);
            if (Type(frame) == "hello") hello = frame;
        }

        return hello ?? throw new Xunit.Sdk.XunitException("handshake did not contain hello");
    }

    private static async Task SendControlAsync(ClientWebSocket socket, string type, string? token = null)
    {
        var frame = new Dictionary<string, object?> { ["type"] = type };
        if (token is not null) frame["token"] = token;
        var bytes = Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeControlFrame(frame));
        await socket.SendAsync(bytes, WebSocketMessageType.Text, true, CancellationToken.None);
    }

    private static async Task<Dictionary<string, object?>> ReceiveUntilAsync(
        ClientWebSocket socket,
        Func<Dictionary<string, object?>, bool> predicate)
    {
        for (var i = 0; i < 12; i++)
        {
            var frame = await ReceiveFrameAsync(socket);
            if (predicate(frame)) return frame;
        }

        throw new Xunit.Sdk.XunitException("expected control frame was not received");
    }

    private static async Task<Dictionary<string, object?>> ReceiveFrameAsync(ClientWebSocket socket)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        var message = await WebSocketMessageReader.ReadAsync(socket, 1_048_576, timeout.Token);
        var decoder = new ControlFrameDecoder();
        var text = Encoding.UTF8.GetString(message.Payload);
        return decoder.Feed(text).OfType<ControlChunk>().First().Control;
    }

    private static string? Type(IReadOnlyDictionary<string, object?> frame) =>
        frame.TryGetValue("type", out var value) ? value?.ToString() : null;

    private static bool Bool(IReadOnlyDictionary<string, object?> frame, string key) =>
        frame.TryGetValue(key, out var value) && value is true;

    private static async Task WaitUntilAsync(Func<bool> predicate)
    {
        for (var i = 0; i < 200 && !predicate(); i++) await Task.Delay(10);
        Assert.True(predicate());
    }

    private static int ResumeTokenCount(UtermServer server)
    {
        var field = typeof(UtermServer).GetField("_resumeTokens", BindingFlags.Instance | BindingFlags.NonPublic)
            ?? throw new Xunit.Sdk.XunitException("resume-token store field was not found");
        return ((ResumeTokenStore)field.GetValue(server)!).Count;
    }

    private static async Task<ClientWebSocket> ConnectAsync(Fixture fixture)
    {
        var socket = new ClientWebSocket();
        socket.Options.SetRequestHeader("Authorization", "Bearer " + fixture.Token);
        await socket.ConnectAsync(fixture.Uri, CancellationToken.None);
        return socket;
    }

    private static async Task<Fixture> BootAsync()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();

        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Auth.Mode = "dev_token";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "resume-worker",
            DisplayName = "Resume Worker",
            Visibility = "public",
            Owner = "admin",
            InputMode = InputModes.Hijack,
            AutoStart = false,
        });
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "resume-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = ["admin"],
        });
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        var worker = new RecordingWorker();
        hub.Conn.RegisterWorker("resume-worker", worker);
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Clock = clock,
        });
        server.Build([$"http://127.0.0.1:{port}"]);
        await server.StartAsync();
        return new Fixture(
            server,
            hub,
            worker,
            token,
            new Uri($"ws://127.0.0.1:{port}/ws/browser/resume-worker/term"));
    }

    private sealed record Fixture(
        UtermServer Server,
        TermHub Hub,
        RecordingWorker Worker,
        string Token,
        Uri Uri);

    private sealed class RecordingWorker : IAbortableBrowserWs
    {
        private readonly object _gate = new();
        private readonly List<string> _actions = [];
        private readonly Queue<PauseGate> _pauseGates = new();
        private PauseGate? _lastPauseGate;
        private TaskCompletionSource? _resumeAttempted;
        private TaskCompletionSource? _resumeRelease;
        private ActiveCheckGate? _nextActiveCheck;
        private bool _isActive = true;

        public Func<Task>? AfterResume { get; set; }

        public bool IsActive
        {
            get
            {
                ActiveCheckGate? activeCheck;
                lock (_gate)
                {
                    activeCheck = _nextActiveCheck;
                    _nextActiveCheck = null;
                    if (activeCheck is null) return _isActive;
                }

                activeCheck.MarkAttempted();
                activeCheck.Wait();
                lock (_gate) return _isActive;
            }
        }

        public IReadOnlyList<string> Actions
        {
            get { lock (_gate) return _actions.ToArray(); }
        }

        public Task PauseAttempted
        {
            get { lock (_gate) return (_lastPauseGate ??= EnqueuePauseGate()).Attempted; }
        }

        public Task ResumeAttempted
        {
            get { lock (_gate) return (_resumeAttempted ??= NewSignal()).Task; }
        }

        public void FailNextPause()
        {
            lock (_gate) _lastPauseGate = EnqueuePauseGate(fail: true, delayed: false);
        }

        public void ThrowAfterNextPause(bool cancel)
        {
            lock (_gate)
            {
                _lastPauseGate = EnqueuePauseGate(
                    fail: !cancel,
                    cancel: cancel,
                    delayed: false,
                    landBeforeFailure: true);
            }
        }

        public PauseGate DelayNextPause(bool fail = false, bool cancel = false)
        {
            lock (_gate)
            {
                return _lastPauseGate = EnqueuePauseGate(fail, cancel, delayed: true);
            }
        }

        public void ReleasePause()
        {
            lock (_gate) _lastPauseGate?.Release();
        }

        public void DelayNextResume()
        {
            lock (_gate)
            {
                _resumeAttempted = NewSignal();
                _resumeRelease = NewSignal();
            }
        }

        public void ReleaseResume()
        {
            lock (_gate) _resumeRelease?.TrySetResult();
        }

        public ActiveCheckGate BlockNextActiveCheck()
        {
            lock (_gate)
            {
                if (_nextActiveCheck is not null)
                {
                    throw new InvalidOperationException("an active check is already blocked");
                }

                return _nextActiveCheck = new ActiveCheckGate();
            }
        }

        public void Deactivate()
        {
            lock (_gate) _isActive = false;
        }

        public void Abort()
        {
            ActiveCheckGate? activeCheck;
            lock (_gate)
            {
                _isActive = false;
                activeCheck = _nextActiveCheck;
                _nextActiveCheck = null;
            }
            activeCheck?.Release();
        }

        public async Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            var action = new ControlFrameDecoder().Feed(payload)
                .OfType<ControlChunk>()
                .Select(chunk => chunk.Control.GetValueOrDefault("action")?.ToString())
                .FirstOrDefault(value => value is not null);
            if (action is not null)
            {
                PauseGate? pauseGate = null;
                Task? release = null;
                TaskCompletionSource? releaseSignal = null;
                lock (_gate)
                {
                    if (action == "pause")
                    {
                        if (_pauseGates.Count > 0) pauseGate = _pauseGates.Dequeue();
                        pauseGate?.MarkAttempted();
                    }
                    else if (action == "resume")
                    {
                        _resumeAttempted?.TrySetResult();
                        releaseSignal = _resumeRelease;
                        release = releaseSignal?.Task;
                    }
                }

                if (pauseGate is not null) await pauseGate.WaitAsync(cancellationToken);
                if (release is not null) await release.WaitAsync(cancellationToken);
                lock (_gate)
                {
                    if (action == "resume" && ReferenceEquals(_resumeRelease, releaseSignal))
                    {
                        _resumeRelease = null;
                    }
                }
                var recorded = false;
                if (pauseGate?.LandBeforeFailure is true)
                {
                    lock (_gate) _actions.Add(action);
                    recorded = true;
                }
                if (pauseGate?.Cancel is true)
                {
                    throw new OperationCanceledException("deterministic pause cancellation", cancellationToken);
                }
                if (pauseGate?.Fail is true) throw new IOException("deterministic pause failure");
                if (!recorded)
                {
                    lock (_gate) _actions.Add(action);
                }
                if (action == "resume" && AfterResume is not null) await AfterResume();
            }
        }

        private static TaskCompletionSource NewSignal() =>
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        private PauseGate EnqueuePauseGate(
            bool fail = false,
            bool cancel = false,
            bool delayed = true,
            bool landBeforeFailure = false)
        {
            var gate = new PauseGate(fail, cancel, delayed, landBeforeFailure);
            _pauseGates.Enqueue(gate);
            return gate;
        }

        public sealed class PauseGate
        {
            private readonly TaskCompletionSource _attempted = NewSignal();
            private readonly TaskCompletionSource? _release;

            internal PauseGate(bool fail, bool cancel, bool delayed, bool landBeforeFailure)
            {
                Fail = fail;
                Cancel = cancel;
                LandBeforeFailure = landBeforeFailure;
                if (delayed) _release = NewSignal();
            }

            public bool Fail { get; }
            public bool Cancel { get; }
            public bool LandBeforeFailure { get; }
            public Task Attempted => _attempted.Task;
            internal void MarkAttempted() => _attempted.TrySetResult();
            public void Release() => _release?.TrySetResult();

            internal Task WaitAsync(CancellationToken cancellationToken) =>
                _release?.Task.WaitAsync(cancellationToken) ?? Task.CompletedTask;
        }

        public sealed class ActiveCheckGate
        {
            private readonly TaskCompletionSource _attempted = NewSignal();
            private readonly TaskCompletionSource _release = NewSignal();

            public Task Attempted => _attempted.Task;
            internal void MarkAttempted() => _attempted.TrySetResult();
            internal void Wait() => _release.Task.GetAwaiter().GetResult();
            public void Release() => _release.TrySetResult();
        }
    }

    private sealed class ClosedWorker : IAbortableBrowserWs
    {
        public bool IsActive => false;
        public int SendCount { get; private set; }
        public void Abort() { }

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            SendCount++;
            return Task.CompletedTask;
        }
    }
}
