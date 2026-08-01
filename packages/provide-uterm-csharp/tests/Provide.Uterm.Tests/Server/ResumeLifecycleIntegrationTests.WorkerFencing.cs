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

public sealed partial class ResumeLifecycleIntegrationTests
{
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
}
