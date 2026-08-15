//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.Hub;

/// <summary>
/// The hub half of the browser-input approval path: parking a held command,
/// resolving it exactly once, injecting it at the generation it was held at, and
/// refusing it when that capability lapsed. Mirrors Go
/// <c>hub/approvals_resolve_test.go</c>.
///
/// Every one of these goes red without the orchestration: before it, the C# port
/// defined <c>approval_pending</c>/<c>approval_resolved</c> and never sent
/// either, and approving a request only flipped a status field.
///
/// Runs in the ~Hub gate batch.
/// </summary>
public class ApprovalResolveTests
{
    private static string? Text(Dictionary<string, object?>? frame, string key) =>
        frame is not null && frame.TryGetValue(key, out var v) ? v?.ToString() : null;

    [Fact]
    public async Task Approve_InjectsHeldCommandOnceAndUnparksTheBrowser()
    {
        var h = ApprovalHarness.Create();

        var requestId = await h.ParkIdAsync("rm -rf /");

        Assert.Empty(h.Worker.Inputs); // parking must not touch the worker
        Assert.True(h.Hub.IsBrowserParked(h.Browser));
        var pending = h.Browser.Frame("approval_pending");
        Assert.Equal(requestId, Text(pending, "request_id"));
        Assert.Equal("rm -rf /", Text(pending, "command"));

        var outcome = await h.Hub.ResolveApprovalAsync(requestId, approve: true, null, "approver");

        Assert.Equal(ApprovalResolution.Resolved, outcome);
        Assert.Equal(["rm -rf /"], h.Worker.Inputs);
        Assert.False(h.Hub.IsBrowserParked(h.Browser));
        var resolved = h.Browser.Frame("approval_resolved");
        Assert.Equal("approved", Text(resolved, "outcome"));
        Assert.Equal(requestId, Text(resolved, "request_id"));
        Assert.Equal(ApprovalStatus.Approved, h.Hub.Approvals.Get(requestId)!.Status);
    }

    [Fact]
    public async Task Reject_SendsTheRedBannerAndNeverInjects()
    {
        var h = ApprovalHarness.Create();
        var requestId = await h.ParkIdAsync("sudo halt");

        var outcome = await h.Hub.ResolveApprovalAsync(requestId, approve: false, "policy", "approver");

        Assert.Equal(ApprovalResolution.Resolved, outcome);
        Assert.Empty(h.Worker.Inputs);
        var banner = Text(h.Browser.Frame("term"), "data");
        Assert.Contains("[REJECTED] Command 'sudo halt' blocked by Admin.", banner, StringComparison.Ordinal);
        Assert.Contains("Reason: policy", banner, StringComparison.Ordinal);
        Assert.Equal("rejected", Text(h.Browser.Frame("approval_resolved"), "outcome"));
        Assert.False(h.Hub.IsBrowserParked(h.Browser));
    }

    [Fact]
    public async Task Reject_WithoutAReasonOmitsTheReasonClause()
    {
        var h = ApprovalHarness.Create();
        var requestId = await h.ParkIdAsync("  spaced  ");

        await h.Hub.ResolveApprovalAsync(requestId, approve: false, null, null);

        var banner = Text(h.Browser.Frame("term"), "data");
        // The command is trimmed and no yellow reason clause is appended.
        Assert.Contains("Command 'spaced' blocked by Admin.", banner, StringComparison.Ordinal);
        Assert.DoesNotContain("Reason:", banner, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Resolve_IsOneShot()
    {
        var h = ApprovalHarness.Create();
        var requestId = await h.ParkIdAsync("echo hi");

        var first = await h.Hub.ResolveApprovalAsync(requestId, approve: true, null, null);
        var second = await h.Hub.ResolveApprovalAsync(requestId, approve: true, null, null);
        var third = await h.Hub.ResolveApprovalAsync(requestId, approve: false, null, null);

        Assert.Equal(ApprovalResolution.Resolved, first);
        Assert.Equal(ApprovalResolution.NotPending, second);
        Assert.Equal(ApprovalResolution.NotPending, third);
        Assert.Single(h.Worker.Inputs);
        Assert.Equal(
            ApprovalResolution.NotPending,
            await h.Hub.ResolveApprovalAsync("does-not-exist", approve: true, null, null));
    }

    [Fact]
    public async Task Approve_ReplaysWhatTheParkedBrowserTypedMeanwhile()
    {
        var h = ApprovalHarness.Create();
        var requestId = await h.ParkIdAsync("cmd\n");

        Assert.False(h.Hub.HoldBrowserInput(h.Browser, "extra\n"));
        Assert.Empty(h.Worker.Inputs); // held keystrokes wait for the decision

        Assert.Equal(
            ApprovalResolution.Resolved,
            await h.Hub.ResolveApprovalAsync(requestId, approve: true, null, null));

        Assert.Equal(["cmd\n", "extra\n"], h.Worker.Inputs);
    }

    [Fact]
    public async Task Reject_DiscardsTheHoldBufferUnreplayed()
    {
        var h = ApprovalHarness.Create();
        var requestId = await h.ParkIdAsync("cmd\n");
        Assert.False(h.Hub.HoldBrowserInput(h.Browser, "extra\n"));

        await h.Hub.ResolveApprovalAsync(requestId, approve: false, null, null);

        Assert.Empty(h.Worker.Inputs);
        Assert.False(h.Hub.IsBrowserParked(h.Browser));
        // The buffer is gone, not merely unsent: a later hold starts empty.
        Assert.Equal(string.Empty, h.Hub.ReleaseParkedBrowser(h.Browser, replay: true));
    }

    [Fact]
    public async Task Approve_AfterTheSubmitterLostTheLease_RefusesCommandAndReplay()
    {
        var h = ApprovalHarness.Create();
        var requestId = await h.ParkIdAsync("cmd\n");
        Assert.False(h.Hub.HoldBrowserInput(h.Browser, "buffered\n"));
        var (released, _) = await h.Hub.Lease.TryReleaseWsAsync(ApprovalHarness.WorkerId, h.Browser);
        Assert.True(released);

        var outcome = await h.Hub.ResolveApprovalAsync(requestId, approve: true, null, "approver");

        Assert.Equal(ApprovalResolution.Refused, outcome);
        Assert.Empty(h.Worker.Inputs);
        Assert.Equal(ApprovalStatus.Refused, h.Hub.Approvals.Get(requestId)!.Status);
        Assert.Equal("refused", Text(h.Browser.Frame("approval_resolved"), "outcome"));
        Assert.False(h.Hub.IsBrowserParked(h.Browser));
    }

    [Fact]
    public async Task Approve_AfterTheSameBrowserReacquires_StillRefusesTheStaleGeneration()
    {
        // Same browser, same worker, but a new lease: the generation the command
        // was held at is gone, so the decision no longer authorizes anything.
        var h = ApprovalHarness.Create();
        var requestId = await h.ParkIdAsync("stale\n");
        Assert.True((await h.Hub.Lease.TryReleaseWsAsync(ApprovalHarness.WorkerId, h.Browser)).Released);
        var (reacquired, reason) = h.Hub.Lease.TryAcquireWs(ApprovalHarness.WorkerId, h.Browser);
        Assert.True(reacquired, reason);

        var outcome = await h.Hub.ResolveApprovalAsync(requestId, approve: true, null, null);

        Assert.Equal(ApprovalResolution.Refused, outcome);
        Assert.Empty(h.Worker.Inputs);
    }

    [Fact]
    public async Task Approve_WithNoOriginBrowser_IsRefusedRatherThanCrashing()
    {
        // An approval seeded straight into the store (no browser ever parked)
        // cannot own the input path, so it can never be injected.
        var h = ApprovalHarness.Create();
        Assert.True(h.Hub.Approvals.Add(new ApprovalRequest
        {
            Id = "orphan",
            WorkerId = ApprovalHarness.WorkerId,
            SubmitterId = "someone",
            Command = "ls",
            CreatedAt = h.Clock.Wall(),
            ExpiresAt = h.Clock.Wall() + 60,
        }));

        var outcome = await h.Hub.ResolveApprovalAsync("orphan", approve: true, null, null);

        Assert.Equal(ApprovalResolution.Refused, outcome);
        Assert.Empty(h.Worker.Inputs);
        Assert.Equal(ApprovalStatus.Refused, h.Hub.Approvals.Get("orphan")!.Status);
    }

    [Fact]
    public async Task Approve_WhenOnlyTheReplayFails_StaysApproved()
    {
        // The command an admin decided on did reach the worker. Only the
        // submitter's buffered keystrokes did not, which is not a refusal.
        var logs = new List<string>();
        var h = ApprovalHarness.Create(
            worker: new ReplayFailingWorker(),
            onLog: (level, message) => logs.Add(level + ":" + message));
        var requestId = await h.ParkIdAsync("command\n");
        Assert.False(h.Hub.HoldBrowserInput(h.Browser, "replay\n"));

        var outcome = await h.Hub.ResolveApprovalAsync(requestId, approve: true, null, "approver");

        Assert.Equal(ApprovalResolution.Resolved, outcome);
        Assert.Equal(ApprovalStatus.Approved, h.Hub.Approvals.Get(requestId)!.Status);
        Assert.Contains(logs, line => line.Contains("approval_replay_failed", StringComparison.Ordinal));
    }

    [Fact]
    public async Task Resolve_UnderConcurrency_InjectsExactlyOnce()
    {
        var h = ApprovalHarness.Create();
        var requestId = await h.ParkIdAsync("go");

        var results = await Task.WhenAll(Enumerable.Range(0, 16).Select(_ =>
            Task.Run(() => h.Hub.ResolveApprovalAsync(requestId, approve: true, null, null))));

        Assert.Single(results, r => r == ApprovalResolution.Resolved);
        Assert.Single(h.Worker.Inputs);
    }

    [Fact]
    public async Task FreshInputCannotOvertakeTheApprovedCommandAndItsReplay()
    {
        // The browser is unparked and its buffer taken only after the worker is
        // reserved, so a keystroke racing the decision joins the replay or waits
        // behind the batch — it can never land between the command and its replay.
        var worker = new GatedWorker();
        var h = ApprovalHarness.Create(worker: worker);
        var requestId = await h.ParkIdAsync("command\n");
        Assert.False(h.Hub.HoldBrowserInput(h.Browser, "replay\n"));

        var resolve = Task.Run(() => h.Hub.ResolveApprovalAsync(requestId, approve: true, null, null));
        await worker.Entered.Task.WaitAsync(TimeSpan.FromSeconds(20));

        var (generation, allowed) = h.Hub.BrowserInputFence(ApprovalHarness.WorkerId, h.Browser);
        Assert.True(allowed);
        var fresh = Task.Run(() => h.Hub.Lease.SendBrowserInputAtGenerationAsync(
            ApprovalHarness.WorkerId, h.Browser, generation, "fresh\n"));

        worker.Release.TrySetResult();

        Assert.Equal(ApprovalResolution.Resolved, await resolve.WaitAsync(TimeSpan.FromSeconds(20)));
        Assert.True(await fresh.WaitAsync(TimeSpan.FromSeconds(20)));
        Assert.Equal(["command\n", "replay\n", "fresh\n"], h.Worker.Inputs);
    }

    [Fact]
    public async Task AResolverThatOutlivedPruningPublishesNothingForTheReusedId()
    {
        // The final compare-and-set is against the store-assigned revision. A
        // resolver still in flight when its record is pruned and its id reused
        // must not stamp its verdict — or broadcast an outcome — for the
        // replacement.
        var worker = new GatedWorker();
        var h = ApprovalHarness.Create(worker: worker);
        const string requestId = "reused";
        await h.ParkIdAsync("old-command\n", new PolicyDecision
        {
            Action = PolicyActions.Hold,
            TimeoutS = 60,
            RequestId = requestId,
        });

        var resolve = Task.Run(() => h.Hub.ResolveApprovalAsync(requestId, approve: true, null, null));
        await worker.Entered.Task.WaitAsync(TimeSpan.FromSeconds(20));

        h.Clock.SetWall(h.Clock.Wall() + 60 + 3600 + 1);
        h.Hub.Approvals.CleanupExpired();
        Assert.True(h.Hub.Approvals.Add(new ApprovalRequest
        {
            Id = requestId,
            WorkerId = "replacement-worker",
            SubmitterId = "someone",
            Command = "replacement",
            CreatedAt = h.Clock.Wall(),
            ExpiresAt = h.Clock.Wall() + 60,
        }));

        worker.Release.TrySetResult();
        Assert.Equal(ApprovalResolution.Resolved, await resolve.WaitAsync(TimeSpan.FromSeconds(20)));

        var current = h.Hub.Approvals.Get(requestId)!;
        Assert.Equal(ApprovalStatus.Pending, current.Status);
        Assert.Equal("replacement", current.Command);
        Assert.Null(h.Browser.Frame("approval_resolved"));
    }

    [Fact]
    public async Task AStaleResolverDoesNotPublishARefusalForTheReusedId()
    {
        // The refusal branch takes the same care as the success branch: a
        // resolver whose record was pruned and whose id was reused while it
        // waited must not stamp "refused" on the replacement, nor tell the
        // browsers a request they never made was refused.
        var worker = new GatedWorker();
        var h = ApprovalHarness.Create(worker: worker);
        const string requestId = "reused-refusal";
        await h.ParkIdAsync("cmd\n", new PolicyDecision
        {
            Action = PolicyActions.Hold,
            TimeoutS = 60,
            RequestId = requestId,
        });

        // Occupy the worker reservation so the resolver has to wait for it.
        var blocking = Task.Run(() => h.Hub.Lease.SendBrowserInputAsync(
            ApprovalHarness.WorkerId, h.Browser, "block\n"));
        await worker.Entered.Task.WaitAsync(TimeSpan.FromSeconds(20));

        var resolve = Task.Run(() => h.Hub.ResolveApprovalAsync(requestId, approve: true, null, null));
        await WaitUntilAsync(() => h.Hub.Approvals.Get(requestId)?.Status == ApprovalStatus.Approved);

        // The lease moves, so the waiting resolver will find its generation
        // stale and deliver nothing; then the record is pruned and its id reused.
        lock (h.Hub.SharedLock)
        {
            h.Hub.Registry.Get(ApprovalHarness.WorkerId)!.HijackOwnershipVersion++;
        }

        h.Clock.SetWall(h.Clock.Wall() + 60 + 3600 + 1);
        h.Hub.Approvals.CleanupExpired();
        Assert.True(h.Hub.Approvals.Add(new ApprovalRequest
        {
            Id = requestId,
            WorkerId = "replacement-worker",
            SubmitterId = "someone",
            Command = "replacement",
            CreatedAt = h.Clock.Wall(),
            ExpiresAt = h.Clock.Wall() + 60,
        }));

        worker.Release.TrySetResult();
        Assert.True(await blocking.WaitAsync(TimeSpan.FromSeconds(20)));
        Assert.Equal(ApprovalResolution.Resolved, await resolve.WaitAsync(TimeSpan.FromSeconds(20)));

        var current = h.Hub.Approvals.Get(requestId)!;
        Assert.Equal(ApprovalStatus.Pending, current.Status);
        Assert.Equal("replacement", current.Command);
        Assert.Null(h.Browser.Frame("approval_resolved"));
        Assert.Equal(["block\n"], h.Worker.Inputs);
    }

    private static async Task WaitUntilAsync(Func<bool> condition)
    {
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(20);
        while (!condition() && DateTime.UtcNow < deadline)
        {
            await Task.Delay(5);
        }

        Assert.True(condition());
    }

    [Fact]
    public async Task Resolve_DoesNotPublishAnOutcomeForAReusedId()
    {
        // The final compare-and-set is against the store-assigned revision, not
        // the id: a resolver that outlived pruning must not stamp its verdict on
        // whatever request now holds the same id.
        var h = ApprovalHarness.Create();
        var requestId = await h.ParkIdAsync("old\n");
        var claimed = h.Hub.Approvals.Get(requestId)!;

        Assert.True(h.Hub.Approvals.ClaimRevision(requestId, claimed.Revision, ApprovalStatus.Approved));
        // Prune the settled record, then let the id be reused by a fresh request.
        h.Clock.SetWall(h.Clock.Wall() + 60 + 3600 + 1);
        h.Hub.Approvals.CleanupExpired();
        Assert.True(h.Hub.Approvals.Add(new ApprovalRequest
        {
            Id = requestId,
            WorkerId = "replacement-worker",
            SubmitterId = "someone",
            Command = "replacement",
            CreatedAt = h.Clock.Wall(),
            ExpiresAt = h.Clock.Wall() + 60,
        }));

        Assert.False(h.Hub.Approvals.SetStatusRevision(requestId, claimed.Revision, ApprovalStatus.Approved));
        var current = h.Hub.Approvals.Get(requestId)!;
        Assert.Equal(ApprovalStatus.Pending, current.Status);
        Assert.Equal("replacement", current.Command);
    }

    [Fact]
    public async Task PendingApprovals_ReflectsParkAndResolve()
    {
        var h = ApprovalHarness.Create();
        Assert.Empty(h.Hub.Approvals.PendingApprovals());

        var requestId = await h.ParkIdAsync("x");

        Assert.Equal([requestId], h.Hub.Approvals.PendingApprovals().Select(r => r.Id));

        await h.Hub.ResolveApprovalAsync(requestId, approve: true, null, null);

        Assert.Empty(h.Hub.Approvals.PendingApprovals());
    }
}
