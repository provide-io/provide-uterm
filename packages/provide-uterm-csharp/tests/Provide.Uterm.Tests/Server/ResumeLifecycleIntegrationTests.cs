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
using Provide.Uterm.Shell;
using Provide.Uterm.Tunnel;
using Provide.Uterm.TunnelClient;

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
    public async Task DashboardReleaseResumeBlocksSuccessorUntilSendCompletes()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var browser = await ConnectAsync(fixture);
        await DrainHandshakeAsync(browser);
        await SendControlAsync(browser, "hijack_request");
        await ReceiveUntilAsync(browser, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        fixture.Worker.DelayNextResume();

        await SendControlAsync(browser, "hijack_release");
        await fixture.Worker.ResumeAttempted.WaitAsync(TimeSpan.FromSeconds(5));
        (bool Ok, string Reason) successor;
        try
        {
            successor = await fixture.Hub.Lease.TryAcquireRestAsync(
                "resume-worker", "successor", 30, "after-dashboard-release", 10);
        }
        finally
        {
            fixture.Worker.ReleaseResume();
        }

        Assert.False(successor.Ok);
        await ReceiveUntilAsync(browser, frame => Type(frame) == "hijack_state" && !Bool(frame, "hijacked"));
        Assert.True((await fixture.Hub.Lease.TryAcquireRestAsync(
            "resume-worker", "successor", 30, "after-dashboard-release", 11)).Ok);
        Assert.Equal(["pause", "resume", "pause"], fixture.Worker.Actions);
    }

    [Fact]
    public async Task RestReleaseResumeBlocksSuccessorUntilSendCompletes()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        Assert.True((await fixture.Hub.Lease.TryAcquireRestAsync(
            "resume-worker", "rest-owner", 30, "released-rest", 10)).Ok);
        var successor = new RecordingBrowser();
        fixture.Hub.Conn.RegisterBrowser("resume-worker", successor, "admin");
        fixture.Worker.DelayNextResume();
        using var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + fixture.Token);

        var release = http.PostAsync(
            "/worker/resume-worker/hijack/released-rest/release", new StringContent(""));
        await fixture.Worker.ResumeAttempted.WaitAsync(TimeSpan.FromSeconds(5));
        var raced = fixture.Hub.Lease.TryAcquireWsAsync("resume-worker", successor);
        try
        {
            await Task.Delay(50);
            Assert.False(raced.IsCompleted);
            Assert.Null(fixture.Hub.Registry.Get("resume-worker")!.HijackOwner);
        }
        finally
        {
            fixture.Worker.ReleaseResume();
        }

        (await release).EnsureSuccessStatusCode();
        Assert.True((await raced).Ok);
        Assert.Equal(["pause", "resume", "pause"], fixture.Worker.Actions);
    }

    [Fact]
    public async Task ForceReleaseResumeBlocksSuccessorUntilSendCompletes()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        Assert.True((await fixture.Hub.Lease.TryAcquireRestAsync(
            "resume-worker", "rest-owner", 30, "forced-rest", 10)).Ok);
        fixture.Worker.DelayNextResume();

        var release = fixture.Hub.Conn.ForceReleaseHijackAsync("resume-worker");
        await fixture.Worker.ResumeAttempted.WaitAsync(TimeSpan.FromSeconds(5));
        (bool Ok, string Reason) raced;
        try
        {
            raced = await fixture.Hub.Lease.TryAcquireRestAsync(
                "resume-worker", "successor", 30, "after-force", 11);
        }
        finally
        {
            fixture.Worker.ReleaseResume();
        }

        Assert.False(raced.Ok);
        Assert.True(await release);
        Assert.True((await fixture.Hub.Lease.TryAcquireRestAsync(
            "resume-worker", "successor", 30, "after-force", 12)).Ok);
        Assert.Equal(["pause", "resume", "pause"], fixture.Worker.Actions);
    }

    [Fact]
    public async Task WorkerReplacementFencesOldTransportAndClearsInheritedLease()
    {
        var clock = new ManualClock(100);
        clock.SetMonotonic(10);
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        var oldWorker = new RecordingWorker();
        var replacement = new RecordingWorker();
        Assert.True(hub.Conn.RegisterWorker("replace", oldWorker));
        Assert.True((await hub.Lease.TryAcquireRestAsync(
            "replace", "old-owner", 30, "old-lease", 10)).Ok);
        oldWorker.DelayNextResume();

        var replacing = Task.Run(() => hub.Conn.RegisterWorker("replace", replacement));
        await oldWorker.ResumeAttempted.WaitAsync(TimeSpan.FromSeconds(5));
        var state = hub.Registry.Get("replace")!;
        Assert.Same(replacement, state.WorkerWs);
        Assert.Null(state.HijackSession);
        Assert.NotNull(state.HijackPending);
        Assert.False((await hub.Lease.TryAcquireRestAsync(
            "replace", "new-owner", 30, "new-lease", 11)).Ok);
        oldWorker.ReleaseResume();

        Assert.True(await replacing);
        Assert.False(oldWorker.IsActive);
        Assert.Null(state.HijackPending);
        var identityAwareSnapshot = typeof(ConnectionManager).GetMethod(
            "UpdateLastSnapshot",
            [typeof(string), typeof(IWorkerWs), typeof(Dictionary<string, object?>)]);
        Assert.NotNull(identityAwareSnapshot);
        var accepted = (bool)identityAwareSnapshot!.Invoke(
            hub.Conn,
            ["replace", oldWorker, new Dictionary<string, object?> { ["screen"] = "stale" }])!;
        Assert.False(accepted);
        Assert.Null(state.LastSnapshot);
        Assert.Equal(["pause", "resume"], oldWorker.Actions);
        Assert.Empty(replacement.Actions);
    }

    [Fact]
    public async Task StaleLocalWorkerProductionPathCannotPublishAfterReplacement()
    {
        var hub = new TermHub();
        var browser = new RecordingBrowser();
        hub.Conn.RegisterBrowser("stale-local", browser, "viewer");
        var connector = new UshellConnector("stale-local", new UshellConnectorConfig
        {
            PollSleep = _ => { },
        });
        connector.Start();
        var oldWorker = new LocalWorkerLink(hub, "stale-local", connector);
        Assert.True(await oldWorker.AttachAsync(InputModes.Open));
        var state = hub.Registry.Get("stale-local")!;
        var originalSnapshot = state.LastSnapshot!["screen"]?.ToString();
        var originalEventSeq = state.EventSeq;
        browser.Clear();

        Assert.True(hub.Conn.RegisterWorker("stale-local", new RecordingWorker()));
        Assert.False(Assert.IsAssignableFrom<IAbortableBrowserWs>(oldWorker).IsActive);
        await oldWorker.SendTextAsync("stale-local-output");

        Assert.Equal(originalSnapshot, state.LastSnapshot!["screen"]?.ToString());
        Assert.Equal(originalEventSeq, state.EventSeq);
        Assert.Empty(browser.Payloads);
        connector.Stop();
    }

    [Fact]
    public async Task StaleTunnelProductionPathCannotMutateOrPublishAfterReplacement()
    {
        var hub = new TermHub();
        var oldWorker = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker("stale-tunnel", oldWorker));
        hub.Conn.RegisterBrowser("stale-tunnel", browser, "viewer");
        Assert.True(hub.Conn.RegisterWorker("stale-tunnel", new RecordingWorker()));
        var server = NewUnstartedServer(hub);
        var state = hub.Registry.Get("stale-tunnel")!;

        await server.ProcessTunnelFrameAsync(
            "stale-tunnel",
            oldWorker,
            TunnelCodec.DecodeFrame(TunnelCodec.EncodeControl(new Dictionary<string, object?>
            {
                ["type"] = "open",
                ["input_mode"] = InputModes.Open,
            })));
        await server.ProcessTunnelFrameAsync(
            "stale-tunnel",
            oldWorker,
            TunnelCodec.DecodeFrame(TunnelCodec.EncodeControl(new Dictionary<string, object?>
            {
                ["type"] = "snapshot",
                ["screen"] = "stale-tunnel-screen",
            })));
        await server.ProcessTunnelFrameAsync(
            "stale-tunnel",
            oldWorker,
            TunnelCodec.DecodeFrame(TunnelCodec.EncodeFrame(
                TunnelProtocol.ChannelData,
                Encoding.UTF8.GetBytes("stale-tunnel-output"))));

        Assert.Equal(InputModes.Hijack, state.InputMode);
        Assert.Null(state.LastSnapshot);
        Assert.Equal(0, state.EventSeq);
        Assert.Empty(browser.Payloads);
    }

    [Fact]
    public async Task StaleNormalWorkerProductionPathCannotMutateOrPublishAfterReplacement()
    {
        var hub = new TermHub();
        var oldWorker = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker("stale-normal", oldWorker));
        hub.Conn.RegisterBrowser("stale-normal", browser, "viewer");
        Assert.True(hub.Conn.RegisterWorker("stale-normal", new RecordingWorker()));
        var server = NewUnstartedServer(hub);
        var state = hub.Registry.Get("stale-normal")!;

        await server.ProcessWorkerChunkAsync(
            "stale-normal",
            oldWorker,
            new ControlChunk(new Dictionary<string, object?>
            {
                ["type"] = "snapshot",
                ["screen"] = "stale-normal-screen",
            }));
        await server.ProcessWorkerChunkAsync(
            "stale-normal",
            oldWorker,
            new ControlChunk(new Dictionary<string, object?>
            {
                ["type"] = "worker_hello",
                ["input_mode"] = InputModes.Open,
            }));
        await server.ProcessWorkerChunkAsync(
            "stale-normal", oldWorker, new DataChunk("stale-normal-output"));

        Assert.Equal(InputModes.Hijack, state.InputMode);
        Assert.Null(state.LastSnapshot);
        Assert.Equal(0, state.EventSeq);
        Assert.Empty(browser.Payloads);
    }

    [Theory]
    [InlineData(true, "release")]
    [InlineData(true, "expiry")]
    [InlineData(true, "replacement")]
    [InlineData(false, "release")]
    [InlineData(false, "expiry")]
    [InlineData(false, "replacement")]
    public async Task AuthorizedInputSerializesWithLifecycleTransition(
        bool restInput,
        string transitionKind)
    {
        var clock = new ManualClock(100);
        clock.SetMonotonic(0);
        var hub = new TermHub(new TermHubConfig { Clock = clock, DashboardHijackLeaseS = 1 });
        var oldWorker = new RecordingWorker();
        var replacement = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker("input-race", oldWorker));
        hub.Conn.RegisterBrowser("input-race", browser, "admin");
        var leaseS = transitionKind == "expiry" ? 1 : 30;
        if (restInput)
        {
            Assert.True((await hub.Lease.TryAcquireRestAsync(
                "input-race", "rest-owner", leaseS, "input-lease", 0)).Ok);
        }
        else
        {
            Assert.True((await hub.Lease.TryAcquireWsAsync("input-race", browser)).Ok);
        }

        oldWorker.DelayNextInput();
        var server = NewUnstartedServer(hub);
        var input = restInput
            ? SendRestInputAsync()
            : server.SendBrowserInputAsync("input-race", browser, "old-owner-input");
        await oldWorker.InputAttempted.WaitAsync(TimeSpan.FromSeconds(5));

        var transition = RunTransitionAsync();
        var completedBeforeInput = transition.IsCompleted;
        oldWorker.ReleaseInput();
        Assert.True(await input);
        await transition;

        Assert.False(completedBeforeInput);
        Assert.Contains("old-owner-input", oldWorker.Inputs);
        Assert.DoesNotContain("old-owner-input", replacement.Inputs);

        async Task<bool> SendRestInputAsync() =>
            (await hub.Conn.SendRestInputAsync(
                "input-race", "input-lease", "old-owner-input")).Ok;

        async Task RunTransitionAsync()
        {
            switch (transitionKind)
            {
                case "release" when restInput:
                    _ = await hub.Lease.ReleaseRestAsync("input-race", "input-lease");
                    break;
                case "release":
                    _ = await hub.Lease.TryReleaseWsAsync("input-race", browser);
                    break;
                case "expiry":
                    clock.SetMonotonic(2);
                    _ = await hub.Lease.CleanupExpiredAsync("input-race");
                    break;
                case "replacement":
                    Assert.True(await hub.Conn.RegisterWorkerAsync("input-race", replacement));
                    break;
                default:
                    throw new Xunit.Sdk.XunitException("unknown transition kind");
            }
        }
    }

    [Theory]
    [InlineData("release")]
    [InlineData("expiry")]
    [InlineData("replacement")]
    public async Task LifecycleTransitionIntentPrecedesLaterAuthorizedInput(string transitionKind)
    {
        var clock = new ManualClock(100);
        clock.SetMonotonic(0);
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        var oldWorker = new RecordingWorker();
        var replacement = new RecordingWorker();
        Assert.True(hub.Conn.RegisterWorker("transition-priority", oldWorker));
        Assert.True((await hub.Lease.TryAcquireRestAsync(
            "transition-priority",
            "rest-owner",
            transitionKind == "expiry" ? 1 : 30,
            "transition-lease",
            0)).Ok);

        oldWorker.DelayNextInput();
        var first = hub.Conn.SendRestInputAsync(
            "transition-priority", "transition-lease", "input-a");
        await oldWorker.InputAttempted.WaitAsync(TimeSpan.FromSeconds(5));

        if (transitionKind == "expiry") clock.SetMonotonic(2);
        var transition = RunTransitionAsync();
        var transitionFenceRegistered = hub.Registry.Get("transition-priority")!.HijackPending is not null;
        var second = hub.Conn.SendRestInputAsync(
            "transition-priority", "transition-lease", "input-b");
        var secondCompletedBeforeFirst = second.IsCompleted;

        oldWorker.ReleaseInput();
        Assert.True((await first).Ok);
        await transition;
        var secondResult = await second;

        Assert.True(transitionFenceRegistered);
        Assert.False(secondCompletedBeforeFirst);
        Assert.False(secondResult.Ok);
        Assert.Equal(["input-a"], oldWorker.Inputs);
        Assert.Empty(replacement.Inputs);

        async Task RunTransitionAsync()
        {
            switch (transitionKind)
            {
                case "release":
                    _ = await hub.Lease.ReleaseRestAsync(
                        "transition-priority", "transition-lease");
                    break;
                case "expiry":
                    _ = await hub.Lease.CleanupExpiredAsync("transition-priority");
                    break;
                case "replacement":
                    Assert.True(await hub.Conn.RegisterWorkerAsync(
                        "transition-priority", replacement));
                    break;
                default:
                    throw new Xunit.Sdk.XunitException("unknown transition kind");
            }
        }
    }

    [Fact]
    public async Task DisconnectResumeWaitsForReservedBrowserInputToDrain()
    {
        var hub = new TermHub();
        var worker = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker("disconnect-input", worker));
        hub.Conn.RegisterBrowser("disconnect-input", browser, "admin");
        Assert.True((await hub.Lease.TryAcquireWsAsync("disconnect-input", browser)).Ok);

        worker.DelayNextInput();
        var server = NewUnstartedServer(hub);
        var input = server.SendBrowserInputAsync(
            "disconnect-input", browser, "disconnect-owner-input");
        await worker.InputAttempted.WaitAsync(TimeSpan.FromSeconds(5));

        var ownershipVersion = Assert.IsType<long>(
            hub.Conn.CleanupBrowser("disconnect-input", browser));
        var resume = hub.Conn.ResumeWorkerIfOwnershipUnchangedAsync(
            "disconnect-input",
            ownershipVersion,
            HijackLeaseManager.ResumeFrame("dashboard", 100));
        var resumeCompletedBeforeInput = resume.IsCompleted;

        worker.ReleaseInput();
        Assert.True(await input);
        Assert.True((await resume).Resumed);

        Assert.False(resumeCompletedBeforeInput);
        Assert.Equal(["pause", "resume"], worker.Actions);
        Assert.Equal(["disconnect-owner-input"], worker.Inputs);
    }

    [Fact]
    public async Task CancelledDisconnectResumeClearsDeferredBrowserOwner()
    {
        var hub = new TermHub();
        var worker = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker("disconnect-cancel", worker));
        hub.Conn.RegisterBrowser("disconnect-cancel", browser, "admin");
        Assert.True((await hub.Lease.TryAcquireWsAsync("disconnect-cancel", browser)).Ok);

        worker.DelayNextInput();
        var server = NewUnstartedServer(hub);
        var input = server.SendBrowserInputAsync(
            "disconnect-cancel", browser, "disconnect-cancel-input");
        await worker.InputAttempted.WaitAsync(TimeSpan.FromSeconds(5));

        var ownershipVersion = Assert.IsType<long>(
            hub.Conn.CleanupBrowser("disconnect-cancel", browser));
        using var cancelled = new CancellationTokenSource();
        var resume = hub.Conn.ResumeWorkerIfOwnershipUnchangedAsync(
            "disconnect-cancel",
            ownershipVersion,
            HijackLeaseManager.ResumeFrame("dashboard", 100),
            cancelled.Token);
        cancelled.Cancel();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => resume);
        Assert.Null(hub.Registry.Get("disconnect-cancel")!.HijackOwner);

        worker.ReleaseInput();
        Assert.True(await input);
    }

    [Fact]
    public async Task WorkerDeregistrationCancelsQueuedDisconnectResume()
    {
        var hub = new TermHub();
        var worker = new RecordingWorker();
        var replacement = new RecordingWorker();
        var browser = new RecordingBrowser();
        Assert.True(hub.Conn.RegisterWorker("disconnect-clear", worker));
        hub.Conn.RegisterBrowser("disconnect-clear", browser, "admin");
        Assert.True((await hub.Lease.TryAcquireWsAsync("disconnect-clear", browser)).Ok);

        worker.DelayNextInput();
        var server = NewUnstartedServer(hub);
        var input = server.SendBrowserInputAsync(
            "disconnect-clear", browser, "disconnect-clear-input");
        await worker.InputAttempted.WaitAsync(TimeSpan.FromSeconds(5));

        var replacementTransition = hub.Conn.RegisterWorkerAsync(
            "disconnect-clear", replacement);
        var ownershipVersion = Assert.IsType<long>(
            hub.Conn.CleanupBrowser("disconnect-clear", browser));
        var resume = hub.Conn.ResumeWorkerIfOwnershipUnchangedAsync(
            "disconnect-clear",
            ownershipVersion,
            HijackLeaseManager.ResumeFrame("dashboard", 100));
        Assert.Equal(1, QueuedLifecycleTransitionCount(
            hub.Registry.Get("disconnect-clear")!));

        Assert.True(hub.Conn.DeregisterWorker("disconnect-clear", worker).ShouldBroadcast);
        var result = await resume.WaitAsync(TimeSpan.FromSeconds(1));

        Assert.False(result.Resumed);
        worker.ReleaseInput();
        Assert.True(await input);
        Assert.True(await replacementTransition);
    }

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
        Assert.Empty(ownershipChanges);

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
        Assert.Empty(ownershipChanges);
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
        hub.NotifyHijackChanged(workerId, true, "replacement-owner");
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

    private static IReadOnlyList<Dictionary<string, object?>> DecodeBrowserFrames(RecordingBrowser browser) =>
        DecodeBrowserFrames(browser.Payloads);

    private static IReadOnlyList<Dictionary<string, object?>> DecodeBrowserFrames(
        DelayedDisconnectBrowser browser) => DecodeBrowserFrames(browser.Payloads);

    private static IReadOnlyList<Dictionary<string, object?>> DecodeBrowserFrames(
        IReadOnlyList<string> payloads) =>
        payloads
            .SelectMany(payload => new ControlFrameDecoder().Feed(payload))
            .OfType<ControlChunk>()
            .Select(chunk => chunk.Control)
            .ToArray();

    private static bool Bool(IReadOnlyDictionary<string, object?> frame, string key) =>
        frame.TryGetValue(key, out var value) && value is true;

    private static int QueuedLifecycleTransitionCount(WorkerTermState state)
    {
        var field = typeof(WorkerTermState).GetField(
            "LifecycleTransitionQueue",
            System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
        Assert.NotNull(field);
        var queue = Assert.IsAssignableFrom<System.Collections.ICollection>(field.GetValue(state));
        return queue.Count;
    }

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

    private static UtermServer NewUnstartedServer(TermHub hub)
    {
        var cfg = UtermServerConfig.Default();
        return new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Clock = new RealClock(),
        });
    }

    private static UtermServer NewUnstartedServer(
        TermHub hub,
        string sessionId,
        out InMemorySessionRegistry registry)
    {
        var cfg = UtermServerConfig.Default();
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = sessionId,
            DisplayName = sessionId,
            Visibility = "public",
            InputMode = InputModes.Hijack,
            AutoStart = false,
        });
        registry = new InMemorySessionRegistry(cfg.Sessions);
        return new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = registry,
            Clock = new RealClock(),
        });
    }

    private sealed record Fixture(
        UtermServer Server,
        TermHub Hub,
        RecordingWorker Worker,
        string Token,
        Uri Uri);

    private enum FaultTarget
    {
        Resume,
        Input,
    }

    private sealed class FaultingWorker : IAbortableBrowserWs
    {
        private readonly FaultTarget _target;
        private readonly bool _hangs;
        private readonly TaskCompletionSource _failureAttempted = NewSignal();
        private readonly TaskCompletionSource _throwRelease = NewSignal();
        private readonly TaskCompletionSource _never = NewSignal();

        internal FaultingWorker(FaultTarget target, bool hangs)
        {
            _target = target;
            _hangs = hangs;
        }

        public bool IsActive { get; private set; } = true;

        internal bool Aborted { get; private set; }

        internal bool AbortedWhileAuthoritative { get; private set; }

        internal Func<bool>? IsAuthoritative { get; set; }

        internal Task FailureAttempted => _failureAttempted.Task;

        public void Abort()
        {
            AbortedWhileAuthoritative = IsAuthoritative?.Invoke() is true;
            Aborted = true;
            IsActive = false;
        }

        internal void ReleaseThrow() => _throwRelease.TrySetResult();

        public async Task SendTextAsync(
            string payload,
            CancellationToken cancellationToken = default)
        {
            var action = new ControlFrameDecoder().Feed(payload)
                .OfType<ControlChunk>()
                .Select(chunk => chunk.Control.GetValueOrDefault("action")?.ToString())
                .FirstOrDefault(value => value is not null);
            var shouldFail = _target == FaultTarget.Resume && action == "resume"
                || _target == FaultTarget.Input && action is null;
            if (!shouldFail) return;

            _failureAttempted.TrySetResult();
            if (_hangs)
            {
                await _never.Task.ConfigureAwait(false);
                return;
            }

            await _throwRelease.Task.ConfigureAwait(false);
            throw new IOException("deterministic worker send failure");
        }

        private static TaskCompletionSource NewSignal() =>
            new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    private sealed class AcquisitionFaultWorker : IAbortableBrowserWs
    {
        private readonly string _failedAction;
        private readonly bool _hangs;
        private readonly TaskCompletionSource _pauseAttempted = NewSignal();
        private readonly TaskCompletionSource _pauseRelease = NewSignal();
        private readonly TaskCompletionSource _failureAttempted = NewSignal();
        private readonly TaskCompletionSource _throwRelease = NewSignal();
        private readonly TaskCompletionSource _never = NewSignal();

        internal AcquisitionFaultWorker(string failedAction, bool hangs)
        {
            _failedAction = failedAction;
            _hangs = hangs;
        }

        public bool IsActive { get; private set; } = true;
        internal bool Aborted { get; private set; }
        internal Func<bool>? IsAuthoritative { get; set; }
        internal Task PauseAttempted => _pauseAttempted.Task;
        internal Task FailureAttempted => _failureAttempted.Task;

        public void Abort()
        {
            Aborted = true;
            IsActive = false;
        }

        internal void ReleasePause() => _pauseRelease.TrySetResult();
        internal void ReleaseThrow() => _throwRelease.TrySetResult();

        public async Task SendTextAsync(
            string payload,
            CancellationToken cancellationToken = default)
        {
            var action = new ControlFrameDecoder().Feed(payload)
                .OfType<ControlChunk>()
                .Select(chunk => chunk.Control.GetValueOrDefault("action")?.ToString())
                .FirstOrDefault(value => value is not null);
            if (action == "pause")
            {
                _pauseAttempted.TrySetResult();
                if (_failedAction == "resume")
                {
                    await _pauseRelease.Task.ConfigureAwait(false);
                    return;
                }
            }
            if (action != _failedAction) return;

            _failureAttempted.TrySetResult();
            if (_hangs)
            {
                await _never.Task.ConfigureAwait(false);
                return;
            }

            await _throwRelease.Task.ConfigureAwait(false);
            throw new IOException($"deterministic {action} failure");
        }

        private static TaskCompletionSource NewSignal() =>
            new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    private sealed class RecordingWorker : IAbortableBrowserWs
    {
        private readonly object _gate = new();
        private readonly List<string> _actions = [];
        private readonly Queue<PauseGate> _pauseGates = new();
        private PauseGate? _lastPauseGate;
        private TaskCompletionSource? _resumeAttempted;
        private TaskCompletionSource? _resumeRelease;
        private TaskCompletionSource? _inputAttempted;
        private TaskCompletionSource? _inputRelease;
        private ActiveCheckGate? _nextActiveCheck;
        private bool _isActive = true;
        private readonly List<string> _inputs = [];

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

        public IReadOnlyList<string> Inputs
        {
            get { lock (_gate) return _inputs.ToArray(); }
        }

        public Task InputAttempted
        {
            get { lock (_gate) return (_inputAttempted ??= NewSignal()).Task; }
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

        public void DelayNextInput()
        {
            lock (_gate)
            {
                _inputAttempted = NewSignal();
                _inputRelease = NewSignal();
            }
        }

        public void ReleaseInput()
        {
            lock (_gate) _inputRelease?.TrySetResult();
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
            else
            {
                Task? release;
                TaskCompletionSource? releaseSignal;
                lock (_gate)
                {
                    _inputAttempted?.TrySetResult();
                    releaseSignal = _inputRelease;
                    release = releaseSignal?.Task;
                }

                if (release is not null) await release.WaitAsync(cancellationToken);
                lock (_gate)
                {
                    _inputs.Add(payload);
                    if (ReferenceEquals(_inputRelease, releaseSignal)) _inputRelease = null;
                }
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

    private sealed class RecordingBrowser : IWorkerWs
    {
        private readonly object _gate = new();
        private readonly List<string> _payloads = [];

        public IReadOnlyList<string> Payloads
        {
            get { lock (_gate) return _payloads.ToArray(); }
        }

        public void Clear()
        {
            lock (_gate) _payloads.Clear();
        }

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            lock (_gate) _payloads.Add(payload);
            return Task.CompletedTask;
        }
    }

    private sealed class DelayedDisconnectBrowser : IWorkerWs
    {
        private readonly object _gate = new();
        private readonly List<string> _payloads = [];
        private readonly TaskCompletionSource _disconnectAttempted =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource _disconnectRelease =
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        public Task DisconnectAttempted => _disconnectAttempted.Task;

        public IReadOnlyList<string> Payloads
        {
            get { lock (_gate) return _payloads.ToArray(); }
        }

        public void ReleaseDisconnect() => _disconnectRelease.TrySetResult();

        public async Task SendTextAsync(
            string payload,
            CancellationToken cancellationToken = default)
        {
            lock (_gate) _payloads.Add(payload);
            var isDisconnect = new ControlFrameDecoder().Feed(payload)
                .OfType<ControlChunk>()
                .Any(chunk => Type(chunk.Control) == "worker_disconnected");
            if (!isDisconnect) return;
            _disconnectAttempted.TrySetResult();
            await _disconnectRelease.Task.WaitAsync(cancellationToken);
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

    private sealed class GatedClock : IClock
    {
        private readonly TaskCompletionSource _sleepAttempted =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource _sleepRelease =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private double _monotonic;

        public Task SleepAttempted => _sleepAttempted.Task;
        public double Monotonic() => _monotonic;
        public double Wall() => 100 + _monotonic;
        public void SetMonotonic(double value) => _monotonic = value;
        public void ReleaseSleep() => _sleepRelease.TrySetResult();

        public async Task SleepAsync(double seconds, CancellationToken cancellationToken = default)
        {
            _sleepAttempted.TrySetResult();
            await _sleepRelease.Task.WaitAsync(cancellationToken);
        }
    }
}
