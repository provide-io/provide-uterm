//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package memory_test

import (
	"context"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/memory"
)

func sampleSession(id string) cp.SessionRecord {
	return cp.SessionRecord{
		SessionID: id, DisplayName: "Test", ConnectorType: "pty", Owner: cp.Str("user"),
		Visibility: "private", LifecycleState: "waiting", CreatedAt: 1.0, UpdatedAt: 1.0,
	}
}

func TestRollbackRevertsState(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()
	tx, _ := e.Begin(ctx)
	store := e.SessionStore(tx)
	rec := sampleSession("test-session")
	_ = store.Upsert(ctx, rec)
	got, _ := store.Get(ctx, "test-session")
	if got == nil || *got != rec {
		t.Fatalf("pre-rollback read mismatch: %+v", got)
	}
	_ = tx.Rollback(ctx)

	tx2, _ := e.Begin(ctx)
	if after, _ := e.SessionStore(tx2).Get(ctx, "test-session"); after != nil {
		t.Fatal("state should have reverted after rollback")
	}
}

func TestCommitPersistsState(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()
	tx, _ := e.Begin(ctx)
	rec := sampleSession("test-session-commit")
	_ = e.SessionStore(tx).Upsert(ctx, rec)
	_ = tx.Commit(ctx)

	tx2, _ := e.Begin(ctx)
	if got, _ := e.SessionStore(tx2).Get(ctx, "test-session-commit"); got == nil || *got != rec {
		t.Fatalf("committed state not visible: %+v", got)
	}
}

func TestRollbackDoesNotRevertCommittedConcurrent(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()
	tx1, _ := e.Begin(ctx)
	tx2, _ := e.Begin(ctx)
	rec := sampleSession("committed-while-first-open")
	_ = e.SessionStore(tx2).Upsert(ctx, rec)
	_ = tx2.Commit(ctx)
	_ = tx1.Rollback(ctx)

	read, _ := e.Begin(ctx)
	if got, _ := e.SessionStore(read).Get(ctx, rec.SessionID); got == nil || *got != rec {
		t.Fatalf("concurrent commit lost after other rollback: %+v", got)
	}
}

func TestCommitIdempotentAfterClose(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()
	tx, _ := e.Begin(ctx)
	_ = e.SessionStore(tx).Upsert(ctx, sampleSession("s1"))
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	// Second commit hits the closed early-out.
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
}

func TestCommitAppliesKeyDeletion(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()
	seed, _ := e.Begin(ctx)
	_ = e.SessionStore(seed).Upsert(ctx, sampleSession("s1"))
	_ = seed.Commit(ctx)

	tx, _ := e.Begin(ctx)
	// Remove the key from the working state, then commit — root must drop it.
	mt := tx.(*memory.Transaction)
	delete(mt.State().Sessions, "s1")
	_ = tx.Commit(ctx)

	read, _ := e.Begin(ctx)
	if got, _ := e.SessionStore(read).Get(ctx, "s1"); got != nil {
		t.Fatal("deleted key should be gone from root")
	}
}

func leaseFor(sid, owner string) cp.LeaseRecord {
	return cp.LeaseRecord{
		SessionID: sid, HijackID: "h-" + owner, Owner: owner, LeaseExpiresAt: 10.0, CreatedAt: 1.0,
	}
}

func TestConcurrentLeaseAcquireSingleWinner(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()

	// Both transactions begin (snapshotting the empty table) before either
	// commits — the double-grant window.
	txA, _ := e.Begin(ctx)
	txB, _ := e.Begin(ctx)
	aEmpty, _ := e.LeaseStore(txA).GetLease(ctx, "w1")
	bEmpty, _ := e.LeaseStore(txB).GetLease(ctx, "w1")

	commit := func(tx cp.Tx, empty *cp.LeaseRecord, owner string) bool {
		if empty != nil {
			_ = tx.Rollback(ctx)
			return false
		}
		_ = e.LeaseStore(tx).PutLease(ctx, leaseFor("w1", owner))
		if err := tx.Commit(ctx); err != nil {
			if cp.IsConflict(err) {
				_ = tx.Rollback(ctx)
				return false
			}
			t.Fatalf("unexpected commit error: %v", err)
		}
		return true
	}
	wonA := commit(txA, aEmpty, "a")
	wonB := commit(txB, bEmpty, "b")
	if wonA == wonB {
		t.Fatalf("expected exactly one winner, got A=%v B=%v", wonA, wonB)
	}

	read, _ := e.Begin(ctx)
	final, _ := e.LeaseStore(read).GetLease(ctx, "w1")
	_ = read.Rollback(ctx)
	if final == nil || (final.Owner != "a" && final.Owner != "b") {
		t.Fatalf("persisted lease inconsistent: %+v", final)
	}
}

func TestNonConflictingConcurrentWritesBothCommit(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()
	tx1, _ := e.Begin(ctx)
	tx2, _ := e.Begin(ctx)
	_ = e.LeaseStore(tx1).PutLease(ctx, leaseFor("k1", "a"))
	_ = e.LeaseStore(tx2).PutLease(ctx, leaseFor("k2", "b"))
	if err := tx2.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	// Disjoint keys must not conflict.
	if err := tx1.Commit(ctx); err != nil {
		t.Fatalf("disjoint-key commit should not conflict: %v", err)
	}
	read, _ := e.Begin(ctx)
	store := e.LeaseStore(read)
	k1, _ := store.GetLease(ctx, "k1")
	k2, _ := store.GetLease(ctx, "k2")
	if k1 == nil || k2 == nil {
		t.Fatal("both disjoint writes should persist")
	}
}
