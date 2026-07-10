//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"testing"
)

// Ported from packages/provide-uterm/tests/bridge/test_coordinator_units.py.

func TestCoordinatorSessionNoneWhenUnset(t *testing.T) {
	c := NewHijackCoordinator()
	if c.Session() != nil {
		t.Fatal("expected nil session on empty coordinator")
	}
}

func TestCoordinatorAcquireBlocksDifferentOwner(t *testing.T) {
	c := NewHijackCoordinator()
	if !c.Acquire("alice", 60).OK {
		t.Fatal("alice acquire should succeed")
	}
	rejected := c.Acquire("bob", 60)
	if rejected.OK {
		t.Fatal("bob acquire should be rejected")
	}
	if rejected.Error != "already_hijacked" {
		t.Fatalf("error = %q, want already_hijacked", rejected.Error)
	}
	if rejected.Session == nil || rejected.Session.Owner != "alice" {
		t.Fatalf("rejection should echo alice's live session, got %+v", rejected.Session)
	}
}

func TestCoordinatorAcquireSameOwnerRenews(t *testing.T) {
	c := NewHijackCoordinator()
	first := c.Acquire("alice", 60)
	renewed := c.Acquire("alice", 60)
	if !renewed.OK || !renewed.IsRenewal {
		t.Fatalf("renew should succeed and be flagged: %+v", renewed)
	}
	if renewed.Session.HijackID == first.Session.HijackID {
		t.Fatal("renewal should mint a new hijack_id")
	}
}

func TestCoordinatorAcquireAfterExpiryStartsFresh(t *testing.T) {
	c := NewHijackCoordinator()
	now := 1000.0
	c.AcquireAt("alice", 1, now-10)
	fresh := c.AcquireAt("alice", 60, now)
	if !fresh.OK || fresh.IsRenewal {
		t.Fatalf("expired lease reacquire should be fresh, got %+v", fresh)
	}
}

func TestCoordinatorAcquireClampsLease(t *testing.T) {
	now := 1000.0
	low := NewHijackCoordinator().AcquireAt("a", 0, now)
	if low.Session.LeaseExpiresAt != now+1 {
		t.Fatalf("low clamp: lease=%v want %v", low.Session.LeaseExpiresAt, now+1)
	}
	if low.Session.AcquiredAt != now || low.Session.LastHeartbeat != now {
		t.Fatalf("acquire should stamp both timestamps from now: %+v", low.Session)
	}
	high := NewHijackCoordinator().AcquireAt("a", 10000, now)
	if high.Session.LeaseExpiresAt != now+3600 {
		t.Fatalf("high clamp: lease=%v want %v", high.Session.LeaseExpiresAt, now+3600)
	}
}

func TestCoordinatorHeartbeatNoActiveSession(t *testing.T) {
	r := NewHijackCoordinator().Heartbeat("fake-id", 60, "owner")
	if r.OK || r.Session != nil || r.Error != "not_hijacked" {
		t.Fatalf("expected not_hijacked, got %+v", r)
	}
}

func TestCoordinatorHeartbeatWrongHijackID(t *testing.T) {
	c := NewHijackCoordinator()
	acq := c.Acquire("owner", 60)
	r := c.Heartbeat("wrong-id", 60, "owner")
	if r.OK || r.Error != "hijack_id_mismatch" {
		t.Fatalf("expected hijack_id_mismatch, got %+v", r)
	}
	if r.Session == nil || r.Session.HijackID != acq.Session.HijackID {
		t.Fatal("rejection should echo live session")
	}
}

func TestCoordinatorHeartbeatWrongOwner(t *testing.T) {
	c := NewHijackCoordinator()
	acq := c.Acquire("alice", 60)
	r := c.Heartbeat(acq.Session.HijackID, 60, "bob")
	if r.OK || r.Error != "owner_mismatch" {
		t.Fatalf("expected owner_mismatch, got %+v", r)
	}
	if r.Session == nil || r.Session.HijackID != acq.Session.HijackID {
		t.Fatal("rejection should echo live session")
	}
}

func TestCoordinatorHeartbeatWithoutOwnerSucceeds(t *testing.T) {
	c := NewHijackCoordinator()
	start := 5000.0
	acq := c.AcquireAt("alice", 60, start)
	beat := start + 30
	r := c.HeartbeatAt(acq.Session.HijackID, 60, "", beat)
	if !r.OK {
		t.Fatalf("owner-less heartbeat should succeed: %+v", r)
	}
	if r.Session.LastHeartbeat != beat || r.Session.LeaseExpiresAt != beat+60 {
		t.Fatalf("heartbeat should advance lease + heartbeat: %+v", r.Session)
	}
}

func TestCoordinatorReleaseNoActiveSession(t *testing.T) {
	r := NewHijackCoordinator().Release("fake-id")
	if r.OK || r.Session != nil || r.Error != "not_hijacked" {
		t.Fatalf("expected not_hijacked, got %+v", r)
	}
}

func TestCoordinatorReleaseWrongHijackID(t *testing.T) {
	c := NewHijackCoordinator()
	acq := c.Acquire("owner", 60)
	r := c.Release("wrong-id")
	if r.OK || r.Error != "hijack_id_mismatch" {
		t.Fatalf("expected hijack_id_mismatch, got %+v", r)
	}
	if r.Session == nil || r.Session.HijackID != acq.Session.HijackID {
		t.Fatal("mismatched release should echo live session")
	}
}

func TestCoordinatorReleaseSuccessClearsSession(t *testing.T) {
	c := NewHijackCoordinator()
	acq := c.Acquire("owner", 60)
	r := c.Release(acq.Session.HijackID)
	if !r.OK || r.Session != nil {
		t.Fatalf("release should succeed and clear: %+v", r)
	}
	if c.Session() != nil {
		t.Fatal("session should be nil after release")
	}
}

func TestCoordinatorCanSendInputNoSession(t *testing.T) {
	c := NewHijackCoordinator()
	if c.CanSendInput("") || c.CanSendInput("some-id") {
		t.Fatal("no active session → can_send_input is always false")
	}
}

func TestCoordinatorCanSendInputRightAndWrong(t *testing.T) {
	c := NewHijackCoordinator()
	acq := c.Acquire("owner", 60)
	if c.CanSendInput("wrong-id") {
		t.Fatal("wrong id should be rejected")
	}
	if !c.CanSendInput(acq.Session.HijackID) {
		t.Fatal("matching id should be accepted")
	}
}

func TestCoordinatorSessionReturnsActive(t *testing.T) {
	c := NewHijackCoordinator()
	acq := c.Acquire("owner", 60)
	active := c.Session()
	if active == nil || active.HijackID != acq.Session.HijackID {
		t.Fatalf("Session should return the live lease, got %+v", active)
	}
}

func TestCoordinatorExpiryAtExactBoundary(t *testing.T) {
	start := 1000.0
	// Just before the boundary the lease is still live.
	live := NewHijackCoordinator()
	acq := live.AcquireAt("owner", 1, start)
	boundary := acq.Session.LeaseExpiresAt // start + 1
	if !live.HeartbeatAt(acq.Session.HijackID, 1, "", boundary-0.001).OK {
		t.Fatal("lease should be live just before boundary")
	}
	// At exactly the boundary it is gone (pins <= vs <).
	exp := NewHijackCoordinator()
	acq2 := exp.AcquireAt("owner", 1, start)
	r := exp.HeartbeatAt(acq2.Session.HijackID, 1, "", boundary)
	if r.OK || r.Error != "not_hijacked" {
		t.Fatalf("lease should expire at exact boundary, got %+v", r)
	}
}

func TestCoordinatorExpiredSlotResetsToNil(t *testing.T) {
	c := NewHijackCoordinator()
	start := 2000.0
	acq := c.AcquireAt("owner", 1, start)
	after := start + 10
	if c.HeartbeatAt(acq.Session.HijackID, 1, "", after).Error != "not_hijacked" {
		t.Fatal("first read past expiry should clear the slot")
	}
	if c.HeartbeatAt(acq.Session.HijackID, 1, "", after).Error != "not_hijacked" {
		t.Fatal("second read should stay clean")
	}
	if c.Session() != nil {
		t.Fatal("session should be nil after expiry")
	}
}

func TestCoordinatorSessionPurgesExpiredLazily(t *testing.T) {
	// Acquire far in the past against real monotonic time → already expired.
	c := NewHijackCoordinator()
	c.AcquireAt("owner", 1, monotonicSeconds()-100)
	if c.Session() != nil {
		t.Fatal("expired lease should be purged by the Session accessor")
	}
}
