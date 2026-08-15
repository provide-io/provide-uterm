//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.Hub;

/// <summary>
/// The policy gate, the parked-browser hold buffers, and the capability fence
/// that park and resolve are both measured against. Mirrors Go
/// <c>hub/approvals_test.go</c> + the gate half of
/// <c>hub/approvals_resolve_test.go</c>.
///
/// Runs in the ~Hub gate batch.
/// </summary>
public class ApprovalGateTests
{
    [Fact]
    public void TheDefaultGateIsTheNoOpGate()
    {
        // The whole point of the default: an existing deployment that configures
        // no gate keeps the ungated input path.
        Assert.True(ApprovalHarness.Create().Hub.IsNoOpPolicyGate);
        Assert.False(ApprovalHarness.Create(new HoldGate()).Hub.IsNoOpPolicyGate);
    }

    [Fact]
    public async Task TheNoOpGateAllowsEverything()
    {
        var gate = new NoOpPolicyGate();

        var decision = await gate.InterceptInputAsync(
            "anything", new PolicyContext { WorkerId = "w" });

        Assert.Equal(PolicyActions.Allow, decision.Action);
        Assert.Equal(60, decision.TimeoutS);
        Assert.Null(decision.RequestId);
        Assert.Null(decision.Reason);
    }

    [Fact]
    public void ADecisionAndItsContextCarryTheGateSuppliedShape()
    {
        // The ported shape, field for field: a gate that only sets Action still
        // gets the reference's defaults, and one that fills the rest is carried
        // whole.
        var decision = new PolicyDecision
        {
            Action = PolicyActions.Deny,
            RequestId = "req-1",
            TimeoutS = 5,
            Reason = "denied by policy",
        };
        var context = new PolicyContext
        {
            WorkerId = "w",
            Metadata = new Dictionary<string, object?> { ["tenant"] = "acme" },
        };

        Assert.Equal(PolicyActions.Deny, decision.Action);
        Assert.Equal("req-1", decision.RequestId);
        Assert.Equal(5, decision.TimeoutS);
        Assert.Equal("denied by policy", decision.Reason);
        Assert.Equal("acme", context.Metadata["tenant"]);
        Assert.Equal("anonymous", context.ClientId);
    }

    [Fact]
    public async Task InterceptBrowserInput_RunsTheConfiguredGateWithTheBrowsersContext()
    {
        PolicyContext? seen = null;
        var h = ApprovalHarness.Create(new CapturingGate(ctx => seen = ctx));

        var decision = await h.Hub.InterceptBrowserInputAsync(ApprovalHarness.WorkerId, h.Browser, "ls");

        Assert.Equal(PolicyActions.Hold, decision.Action);
        Assert.NotNull(seen);
        Assert.Equal(ApprovalHarness.WorkerId, seen!.WorkerId);
        Assert.Equal("submitter", seen.ClientId);
        Assert.Equal("admin", seen.Role);
        Assert.Equal("input", seen.Action);
        Assert.Empty(seen.Metadata);
    }

    [Fact]
    public void PreparePolicyContext_FallsBackForAnUnknownWorkerAndAnonymousBrowser()
    {
        var h = ApprovalHarness.Create();

        var ctx = h.Hub.PreparePolicyContext("no-such-worker", new object(), null);

        Assert.Equal("anonymous", ctx.ClientId);
        Assert.Null(ctx.Role);
        Assert.Null(ctx.Action);
    }

    [Fact]
    public void HoldBrowserInput_DiscardsAnOverflowingAppendAndKeepsWhatItHad()
    {
        // MaxBufferChars is clamped to at least MaxInputChars, so both are pinned
        // to exercise the boundary rather than the clamp.
        var h = ApprovalHarness.Create(maxInputChars: 100, maxBufferChars: 100);
        var browser = new object();

        Assert.False(h.Hub.HoldBrowserInput(browser, new string('a', 90)));
        Assert.True(h.Hub.HoldBrowserInput(browser, new string('b', 20)));
        // The rejected append stored nothing, so 90 + 10 still fits.
        Assert.False(h.Hub.HoldBrowserInput(browser, new string('c', 10)));
        Assert.Equal(100, h.Hub.ReleaseParkedBrowser(browser, replay: true).Length);
    }

    [Fact]
    public void MaxBufferCharsIsClampedToAtLeastMaxInputChars()
    {
        // Otherwise a single admissible keystroke could not be buffered at all.
        var h = ApprovalHarness.Create(maxInputChars: 5000, maxBufferChars: 200);

        Assert.Equal(5000, h.Hub.MaxInputChars);
        Assert.Equal(5000, h.Hub.MaxBufferChars);
    }

    [Fact]
    public void TheCharCapsHaveTheReferenceDefaultsAndFloors()
    {
        Assert.Equal(10000, ApprovalHarness.Create().Hub.MaxInputChars);
        Assert.Equal(40000, ApprovalHarness.Create().Hub.MaxBufferChars);
        Assert.Equal(100, ApprovalHarness.Create(maxInputChars: 5, maxBufferChars: 5).Hub.MaxInputChars);
    }

    [Fact]
    public async Task TryHoldBrowserInput_TellsUnparkedApartFromOverflowing()
    {
        var h = ApprovalHarness.Create();

        // Not parked: the caller must carry on with normal fenced delivery
        // rather than dropping the keystroke.
        Assert.Equal((false, false), h.Hub.TryHoldBrowserInput(h.Browser, "fresh"));

        await h.ParkIdAsync("held\n");

        Assert.Equal((true, false), h.Hub.TryHoldBrowserInput(h.Browser, "buffered"));
        Assert.Equal("buffered", h.Hub.ReleaseParkedBrowser(h.Browser, replay: true));
    }

    [Fact]
    public async Task TryHoldBrowserInput_ReportsAnOverflowWhileParked()
    {
        var h = ApprovalHarness.Create(maxInputChars: 100, maxBufferChars: 100);
        await h.ParkIdAsync("held\n");

        Assert.Equal((true, true), h.Hub.TryHoldBrowserInput(h.Browser, new string('z', 200)));
    }

    [Fact]
    public void ReleaseParkedBrowser_IsANoOpForNoBrowser()
    {
        Assert.Equal(string.Empty, ApprovalHarness.Create().Hub.ReleaseParkedBrowser(null, replay: true));
    }

    [Fact]
    public void BrowserInputFence_RefusesEveryWayABrowserCanLoseIt()
    {
        var h = ApprovalHarness.Create();

        var (generation, allowed) = h.Hub.BrowserInputFence(ApprovalHarness.WorkerId, h.Browser);
        Assert.True(allowed);
        Assert.True(generation > 0);

        // Unknown worker.
        Assert.Equal((0L, false), h.Hub.BrowserInputFence("missing", h.Browser));
        // Registered worker, unregistered browser.
        Assert.Equal((0L, false), h.Hub.BrowserInputFence(ApprovalHarness.WorkerId, new object()));
        // Registered browser without the lease.
        var viewer = new RecordingBrowser();
        h.Hub.Conn.RegisterBrowser(ApprovalHarness.WorkerId, viewer, "viewer");
        Assert.Equal((0L, false), h.Hub.BrowserInputFence(ApprovalHarness.WorkerId, viewer));
        // A worker mid two-phase hijack reservation.
        h.Hub.Registry.Get(ApprovalHarness.WorkerId)!.HijackPending = "reserved";
        Assert.Equal((0L, false), h.Hub.BrowserInputFence(ApprovalHarness.WorkerId, h.Browser));
    }

    [Fact]
    public async Task Park_RefusesWhenTheSubmitterMayNoLongerType()
    {
        var h = ApprovalHarness.Create();
        Assert.True((await h.Hub.Lease.TryReleaseWsAsync(ApprovalHarness.WorkerId, h.Browser)).Released);

        var parked = await h.ParkAsync("rm -rf /");

        Assert.Null(parked.RequestId);
        Assert.Equal(ApprovalParkReasons.OwnershipInvalid, parked.Reason);
        Assert.False(h.Hub.IsBrowserParked(h.Browser));
        Assert.Null(h.Browser.Frame("approval_pending"));
    }

    [Fact]
    public async Task Park_RefusesWhenTheGateSuppliesAnIdThatAlreadyExists()
    {
        var h = ApprovalHarness.Create();
        var decision = new PolicyDecision { Action = PolicyActions.Hold, TimeoutS = 60, RequestId = "fixed" };
        Assert.Equal("fixed", await h.ParkIdAsync("first", decision));

        var second = await h.ParkAsync("second", decision);

        Assert.Null(second.RequestId);
        Assert.Equal(ApprovalParkReasons.DuplicateId, second.Reason);
        Assert.Equal("first", h.Hub.Approvals.Get("fixed")!.Command);
    }

    [Fact]
    public async Task Park_RecordsTheSubmitterAndDeadlineFromTheDecision()
    {
        var h = ApprovalHarness.Create();

        var requestId = await h.ParkIdAsync(
            "danger", new PolicyDecision { Action = PolicyActions.Hold, TimeoutS = 15 });

        var stored = h.Hub.Approvals.Get(requestId)!;
        Assert.Equal("submitter", stored.SubmitterId);
        Assert.Equal(h.Clock.Wall() + 15, stored.ExpiresAt);
        Assert.Equal(ApprovalStatus.Pending, stored.Status);
        Assert.Same(h.Browser, stored.OriginBrowser);
    }

    [Fact]
    public async Task Park_UsesTheCallersGenerationOverTheCurrentOne()
    {
        // The gate may have awaited a remote service; the generation captured
        // before that await is the one the decision is about.
        var h = ApprovalHarness.Create();
        var stale = h.Hub.Registry.Get(ApprovalHarness.WorkerId)!.HijackOwnershipVersion - 1;

        var parked = await h.Hub.ParkBrowserForApprovalAsync(
            ApprovalHarness.WorkerId,
            h.Browser,
            "cmd",
            new PolicyDecision { Action = PolicyActions.Hold, TimeoutS = 60 },
            stale);

        Assert.Null(parked.RequestId);
        Assert.Equal(ApprovalParkReasons.OwnershipInvalid, parked.Reason);
    }

    [Fact]
    public async Task AnExpiredApprovalReleasesTheBrowserItWasHolding()
    {
        // Nothing else ever unparks it. Before this wiring a browser whose
        // approval timed out stayed parked forever, buffering keystrokes for a
        // decision that could no longer be made.
        var h = ApprovalHarness.Create();
        var requestId = await h.ParkIdAsync("cmd\n", new PolicyDecision
        {
            Action = PolicyActions.Hold,
            TimeoutS = 1,
        });
        Assert.True(h.Hub.IsBrowserParked(h.Browser));

        h.Clock.SetWall(h.Clock.Wall() + 5);
        h.Hub.Approvals.CleanupExpired();

        Assert.False(h.Hub.IsBrowserParked(h.Browser));
        Assert.Equal(ApprovalStatus.Timeout, h.Hub.Approvals.Get(requestId)!.Status);
        Assert.Equal(
            ApprovalResolution.NotPending,
            await h.Hub.ResolveApprovalAsync(requestId, approve: true, null, null));
    }

    [Fact]
    public async Task AnExpiryFoundOnARead_AlsoReleasesTheBrowser()
    {
        // The store retires deadlines inline, so the release does not wait for
        // the next sweep tick.
        var h = ApprovalHarness.Create();
        await h.ParkIdAsync("cmd\n", new PolicyDecision { Action = PolicyActions.Hold, TimeoutS = 1 });

        h.Clock.SetWall(h.Clock.Wall() + 5);
        Assert.Empty(h.Hub.Approvals.PendingApprovals());

        Assert.False(h.Hub.IsBrowserParked(h.Browser));
    }

    [Fact]
    public void TheStoreRefusesADuplicateIdAndAssignsRisingRevisions()
    {
        var store = new InMemoryApprovalStore(new ManualClock(100));
        var request = new ApprovalRequest
        {
            Id = "a",
            WorkerId = "w",
            SubmitterId = "s",
            Command = "c",
            ExpiresAt = 500,
        };

        Assert.True(store.Add(request));
        Assert.False(store.Add(request));
        Assert.True(store.Add(new ApprovalRequest
        {
            Id = "b",
            WorkerId = "w",
            SubmitterId = "s",
            Command = "c",
            ExpiresAt = 500,
        }));

        Assert.Equal(1, store.Get("a")!.Revision);
        Assert.Equal(2, store.Get("b")!.Revision);
        // A caller cannot mutate the store through the record it was handed.
        store.Get("a")!.Status = ApprovalStatus.Approved;
        Assert.Equal(ApprovalStatus.Pending, store.Get("a")!.Status);
    }

    [Fact]
    public void ClaimRevision_RefusesAStaleRevisionAndAnOverdueRequest()
    {
        var clock = new ManualClock(100);
        var store = new InMemoryApprovalStore(clock);
        store.Add(new ApprovalRequest
        {
            Id = "a",
            WorkerId = "w",
            SubmitterId = "s",
            Command = "c",
            ExpiresAt = 150,
        });
        var revision = store.Get("a")!.Revision;

        Assert.False(store.ClaimRevision("missing", revision, ApprovalStatus.Approved));
        Assert.False(store.ClaimRevision("a", revision + 99, ApprovalStatus.Approved));

        clock.SetWall(200);
        Assert.False(store.ClaimRevision("a", revision, ApprovalStatus.Approved));
        Assert.Equal(ApprovalStatus.Timeout, store.Get("a")!.Status);
        Assert.False(store.SetStatusRevision("missing", revision, ApprovalStatus.Refused));
    }

    private sealed class CapturingGate : IInputPolicyGate
    {
        private readonly Action<PolicyContext> _observe;

        public CapturingGate(Action<PolicyContext> observe) => _observe = observe;

        public Task<PolicyDecision> InterceptInputAsync(
            string data,
            PolicyContext context,
            CancellationToken cancellationToken = default)
        {
            _observe(context);
            return Task.FromResult(new PolicyDecision { Action = PolicyActions.Hold, TimeoutS = 60 });
        }
    }
}
