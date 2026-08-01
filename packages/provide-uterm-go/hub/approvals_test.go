//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"sort"
	"sync"
	"testing"
)

func pendingReq(id string, expiresAt float64) *ApprovalRequest {
	return &ApprovalRequest{
		ID: id, WorkerID: "w1", SubmitterID: "u1", Command: "cmd",
		Status: ApprovalPending, CreatedAt: 0, ExpiresAt: expiresAt,
	}
}

func TestApprovalStoreAddAndGet(t *testing.T) {
	s := NewInMemoryApprovalStore(NewManualClock(1000))
	req := pendingReq("req-1", 1060)
	s.Add(req)
	mustDeepEqual(t, s.Get("req-1"), req, "get returns copied value")
	mustTrue(t, s.Get("nonexistent") == nil, "get missing -> nil")
}

func TestApprovalStoreDuplicateIDCannotReplacePendingRequest(t *testing.T) {
	s := NewInMemoryApprovalStore(NewManualClock(1000))
	original := pendingReq("same", 2000)
	original.Command = "original"
	s.Add(original)
	replacement := pendingReq("same", 3000)
	replacement.Command = "replacement"
	s.Add(replacement)
	if got := s.Get("same"); got == nil || got.Command != "original" {
		t.Fatalf("duplicate request replaced original: %+v", got)
	}
}

func TestApprovalStoreGetAndPendingReturnImmutableCopies(t *testing.T) {
	s := NewInMemoryApprovalStore(NewManualClock(1000))
	s.Add(pendingReq("copy", 2000))
	got := s.Get("copy")
	got.Status = ApprovalApproved
	pending := s.PendingApprovals()
	if len(pending) != 1 {
		t.Fatalf("external Get mutation changed store: %v", pending)
	}
	pending[0].Status = ApprovalRejected
	if stored := s.Get("copy"); stored.Status != ApprovalPending {
		t.Fatalf("pending snapshot mutation changed store: %+v", stored)
	}
}

func TestApprovalResolveSuccess(t *testing.T) {
	s := NewInMemoryApprovalStore(NewManualClock(1000))
	s.Add(pendingReq("req-1", 1060))
	s.Resolve("req-1", ApprovalApproved)
	mustEqual(t, s.Get("req-1").Status, ApprovalApproved, "resolved to approved")
}

func TestApprovalResolveOnlyPending(t *testing.T) {
	s := NewInMemoryApprovalStore(NewManualClock(1000))
	req := pendingReq("req-1", 1060)
	req.Status = ApprovalApproved
	s.Add(req)
	s.Resolve("req-1", ApprovalRejected)
	mustEqual(t, s.Get("req-1").Status, ApprovalApproved, "non-pending unchanged")
}

func TestApprovalResolveMissingIsNoop(t *testing.T) {
	s := NewInMemoryApprovalStore(NewManualClock(1000))
	s.Resolve("ghost", ApprovalApproved) // must not panic
}

func TestApprovalClaimSucceedsExactlyOnce(t *testing.T) {
	s := NewInMemoryApprovalStore(NewManualClock(1000))
	s.Add(pendingReq("r1", 1e12))
	mustTrue(t, s.Claim("r1", ApprovalApproved), "first claim wins")
	mustFalse(t, s.Claim("r1", ApprovalRejected), "second claim loses")
	mustEqual(t, s.Get("r1").Status, ApprovalApproved, "final status approved")
}

func TestApprovalClaimMissingReturnsFalse(t *testing.T) {
	s := NewInMemoryApprovalStore(NewManualClock(1000))
	mustFalse(t, s.Claim("nope", ApprovalApproved), "missing claim false")
}

func TestApprovalCleanupExpiresPendingKeepsValid(t *testing.T) {
	clk := NewManualClock(1000)
	s := NewInMemoryApprovalStore(clk)
	s.Add(pendingReq("exp", 990))    // past -> timeout
	s.Add(pendingReq("valid", 1010)) // future -> stays pending
	s.CleanupExpired()
	mustEqual(t, s.Get("exp").Status, ApprovalTimeout, "expired -> timeout")
	mustEqual(t, s.Get("valid").Status, ApprovalPending, "valid stays pending")
}

func TestApprovalCleanupInvokesOnExpired(t *testing.T) {
	clk := NewManualClock(1000)
	s := NewInMemoryApprovalStore(clk)
	s.Add(pendingReq("e1", 990))
	s.Add(pendingReq("e2", 990))
	var got []string
	s.OnExpired = func(id string) { got = append(got, id) }
	s.CleanupExpired()
	sort.Strings(got)
	mustDeepEqual(t, got, []string{"e1", "e2"}, "both notified")
	mustEqual(t, s.Get("e1").Status, ApprovalTimeout, "e1 timeout")
	mustEqual(t, s.Get("e2").Status, ApprovalTimeout, "e2 timeout")
}

func TestApprovalCleanupPrunesTerminalPastTTL(t *testing.T) {
	clk := NewManualClock(10000)
	s := NewInMemoryApprovalStore(clk)
	// Terminal state, expired more than PRUNE_TTL ago -> pruned.
	old := pendingReq("old", 10000-approvalPruneTTL-1)
	old.Status = ApprovalRejected
	s.Add(old)
	// Terminal state, expired recently -> kept.
	recent := pendingReq("recent", 10000-100)
	recent.Status = ApprovalApproved
	s.Add(recent)
	s.CleanupExpired()
	mustTrue(t, s.Get("old") == nil, "old pruned")
	mustTrue(t, s.Get("recent") != nil, "recent kept")
}

func TestApprovalCleanupNoCallbackIsSafe(t *testing.T) {
	clk := NewManualClock(1000)
	s := NewInMemoryApprovalStore(clk)
	s.Add(pendingReq("e1", 990))
	s.CleanupExpired() // OnExpired nil, must not panic
	mustEqual(t, s.Get("e1").Status, ApprovalTimeout, "timed out")
}

func TestApprovalConcurrentClaimSingleWinner(t *testing.T) {
	s := NewInMemoryApprovalStore(NewManualClock(1000))
	s.Add(pendingReq("race", 1e12))
	var wg sync.WaitGroup
	wins := make([]bool, 8)
	start := make(chan struct{})
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			<-start
			status := ApprovalApproved
			if idx%2 == 1 {
				status = ApprovalRejected
			}
			wins[idx] = s.Claim("race", status)
		}(i)
	}
	close(start)
	wg.Wait()
	count := 0
	for _, w := range wins {
		if w {
			count++
		}
	}
	mustEqual(t, count, 1, "exactly one claimant wins")
}

func TestApprovalDefaultClock(t *testing.T) {
	s := NewInMemoryApprovalStore(nil) // nil -> real clock
	s.Add(pendingReq("x", 1e12))
	s.CleanupExpired()
	mustEqual(t, s.Get("x").Status, ApprovalPending, "not yet expired")
}
