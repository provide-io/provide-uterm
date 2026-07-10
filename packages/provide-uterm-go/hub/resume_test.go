//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"strconv"
	"testing"
)

func seqTokenGen() tokenGen {
	n := 0
	return func() string {
		n++
		return "tok" + strconv.Itoa(n)
	}
}

func TestResumeStoreCreateGetConsume(t *testing.T) {
	clk := NewManualClock(5000)
	clk.SetMonotonic(1000)
	s := NewInMemoryResumeStore(clk, seqTokenGen())

	tok, err := s.Create(bg(), "w1", "operator", 300)
	mustEqual(t, err, nil, "create err")
	mustEqual(t, tok, "tok1", "token")
	mustEqual(t, s.Len(), 1, "len after create")

	sess, err := s.Get(bg(), tok)
	mustEqual(t, err, nil, "get err")
	if sess == nil {
		t.Fatal("expected session")
	}
	mustEqual(t, sess.WorkerID, "w1", "worker")
	mustEqual(t, sess.Role, "operator", "role")
	mustEqual(t, sess.CreatedAt, 1000.0, "created mono")
	mustEqual(t, sess.ExpiresAt, 1300.0, "expires mono")
	mustEqual(t, sess.WallCreatedAt, 5000.0, "wall created")
	mustFalse(t, sess.WasHijackOwner, "not owner initially")

	// Consume revokes single-use.
	got, err := s.Consume(bg(), tok)
	mustEqual(t, err, nil, "consume err")
	if got == nil {
		t.Fatal("expected consumed session")
	}
	mustEqual(t, s.Len(), 0, "len after consume")
	again, _ := s.Consume(bg(), tok)
	if again != nil {
		t.Fatal("second consume must be nil")
	}
}

func TestResumeStoreGetExpiredPrunes(t *testing.T) {
	clk := NewManualClock(5000)
	clk.SetMonotonic(1000)
	s := NewInMemoryResumeStore(clk, seqTokenGen())
	tok, _ := s.Create(bg(), "w1", "viewer", 10)
	clk.SetMonotonic(1011) // past expiry (1010)
	sess, _ := s.Get(bg(), tok)
	if sess != nil {
		t.Fatal("expired token must return nil")
	}
	mustEqual(t, s.Len(), 0, "expired token pruned on get")
}

func TestResumeStoreConsumeExpired(t *testing.T) {
	clk := NewManualClock(5000)
	clk.SetMonotonic(1000)
	s := NewInMemoryResumeStore(clk, seqTokenGen())
	tok, _ := s.Create(bg(), "w1", "viewer", 10)
	clk.SetMonotonic(1011)
	got, _ := s.Consume(bg(), tok)
	if got != nil {
		t.Fatal("consume of expired must be nil")
	}
	// And it was removed by the consume attempt.
	mustEqual(t, s.Len(), 0, "removed by consume")
}

func TestResumeStoreMissingConsume(t *testing.T) {
	s := NewInMemoryResumeStore(NewManualClock(0), seqTokenGen())
	got, _ := s.Consume(bg(), "nope")
	if got != nil {
		t.Fatal("consume of unknown must be nil")
	}
	sess, _ := s.Get(bg(), "nope")
	if sess != nil {
		t.Fatal("get of unknown must be nil")
	}
}

func TestResumeStoreMarkHijackOwner(t *testing.T) {
	clk := NewManualClock(5000)
	clk.SetMonotonic(1000)
	s := NewInMemoryResumeStore(clk, seqTokenGen())
	tok, _ := s.Create(bg(), "w1", "operator", 300)
	mustEqual(t, s.MarkHijackOwner(bg(), tok, true), nil, "mark err")
	sess, _ := s.Get(bg(), tok)
	mustTrue(t, sess.WasHijackOwner, "was owner set")
	// Unknown token is a no-op.
	mustEqual(t, s.MarkHijackOwner(bg(), "nope", true), nil, "mark unknown err")
}

func TestResumeStoreRevoke(t *testing.T) {
	clk := NewManualClock(5000)
	clk.SetMonotonic(1000)
	s := NewInMemoryResumeStore(clk, seqTokenGen())
	tok, _ := s.Create(bg(), "w1", "viewer", 300)
	mustEqual(t, s.Revoke(bg(), tok), nil, "revoke err")
	mustEqual(t, s.Len(), 0, "revoked")
}

func TestResumeStoreCleanupOnCreate(t *testing.T) {
	clk := NewManualClock(5000)
	clk.SetMonotonic(1000)
	s := NewInMemoryResumeStore(clk, seqTokenGen())
	_, _ = s.Create(bg(), "w1", "viewer", 10)
	clk.SetMonotonic(1011)
	// Creating again should prune the expired first token.
	_, _ = s.Create(bg(), "w2", "viewer", 10)
	mustEqual(t, s.Len(), 1, "expired pruned on create")
	mustEqual(t, s.CleanupExpired(), 0, "nothing left to clean")
}

func TestResumeStoreDefaultTokenGen(t *testing.T) {
	s := NewInMemoryResumeStore(nil, nil) // real clock + crypto tokens
	a, _ := s.Create(bg(), "w", "viewer", 300)
	b, _ := s.Create(bg(), "w", "viewer", 300)
	if a == "" || b == "" || a == b {
		t.Fatalf("expected distinct non-empty tokens, got %q %q", a, b)
	}
}
