//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.Hub;

/// <summary>
/// The approval store must retire a deadline on its own.
///
/// CleanupExpired has always existed and nothing in production ever called it:
/// Go ticks it from StartSweeps and Python from sweep_expired_approvals, but the
/// C# port has no sweep at all. A held command therefore stayed Pending forever,
/// and POST /api/approvals/{id}/approve granted one whose deadline had passed
/// arbitrarily long ago. Every read and write path now checks the deadline
/// first, which is what the reference does inline
/// (bridge/hub/approvals.py:109).
///
/// Runs in the ~Hub gate batch.
/// </summary>
public class ApprovalExpiryTests
{
    private static ApprovalRequest Request(string id, double expiresAt) =>
        new()
        {
            Id = id,
            WorkerId = "w1",
            SubmitterId = "submitter",
            Command = "rm -rf /",
            CreatedAt = 0,
            ExpiresAt = expiresAt,
        };

    [Fact]
    public void Claim_RefusesAnOverdueRequest()
    {
        var clock = new ManualClock(100);
        var store = new InMemoryApprovalStore(clock);
        store.Add(Request("apr-1", expiresAt: 150));

        Assert.True(store.Claim("apr-1", ApprovalStatus.Approved));

        // Same request, same store, but past its deadline: must refuse.
        var late = new InMemoryApprovalStore(clock);
        late.Add(Request("apr-2", expiresAt: 150));
        clock.SetWall(200);

        Assert.False(late.Claim("apr-2", ApprovalStatus.Approved));
        Assert.Equal(ApprovalStatus.Timeout, late.Get("apr-2")!.Status);
    }

    [Fact]
    public void Resolve_RefusesAnOverdueRequest()
    {
        var clock = new ManualClock(100);
        var store = new InMemoryApprovalStore(clock);
        store.Add(Request("apr-1", expiresAt: 50));

        store.Resolve("apr-1", ApprovalStatus.Approved);

        Assert.Equal(ApprovalStatus.Timeout, store.Get("apr-1")!.Status);
    }

    [Fact]
    public void PendingApprovals_DropsAnOverdueRequest()
    {
        var clock = new ManualClock(100);
        var store = new InMemoryApprovalStore(clock);
        store.Add(Request("live", expiresAt: 500));
        store.Add(Request("stale", expiresAt: 50));

        var pending = store.PendingApprovals();

        Assert.Equal(["live"], pending.Select(r => r.Id));
        Assert.Equal(ApprovalStatus.Timeout, store.Get("stale")!.Status);
    }

    [Fact]
    public void ExpiryRaisesOnExpiredExactlyOnce()
    {
        var clock = new ManualClock(100);
        var store = new InMemoryApprovalStore(clock);
        var expired = new List<string>();
        store.OnExpired = expired.Add;
        store.Add(Request("apr-1", expiresAt: 50));

        store.PendingApprovals();
        store.PendingApprovals();
        Assert.False(store.Claim("apr-1", ApprovalStatus.Approved));

        // The second read and the claim find it already Timeout, so neither
        // re-notifies: a duplicate would clear a browser's state twice.
        Assert.Equal(["apr-1"], expired);
    }

    [Fact]
    public void ExpiryWithoutASubscriberIsHarmless()
    {
        var clock = new ManualClock(100);
        var store = new InMemoryApprovalStore(clock);
        store.Add(Request("apr-1", expiresAt: 50));

        Assert.Empty(store.PendingApprovals());
        Assert.Equal(ApprovalStatus.Timeout, store.Get("apr-1")!.Status);
    }

    [Fact]
    public void ADeadlineExactlyReachedIsStillLive()
    {
        // The predicate is ExpiresAt < now, matching CleanupExpired's, so a
        // request is live right up to its deadline and not one tick before.
        var clock = new ManualClock(100);
        var store = new InMemoryApprovalStore(clock);
        store.Add(Request("apr-1", expiresAt: 100));

        Assert.Single(store.PendingApprovals());
        Assert.True(store.Claim("apr-1", ApprovalStatus.Approved));
    }

    [Fact]
    public void ClaimOfAnUnknownRequestIsFalse()
    {
        var store = new InMemoryApprovalStore(new ManualClock(100));

        Assert.False(store.Claim("nope", ApprovalStatus.Approved));
        store.Resolve("nope", ApprovalStatus.Approved);
    }
}
