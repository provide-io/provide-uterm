//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package memory

import (
	"context"
	"sync"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
)

// Transaction is a snapshot-isolated in-memory transaction. Port of
// control.plane.memory.transaction.MemoryTransaction.
//
// On begin it captures a snapshot of the root tables and an independent working
// copy (State). Stores mutate the working copy. Commit takes the shared lock,
// runs optimistic-concurrency conflict detection across all tables, and — only
// if conflict-free — merges this transaction's key-level changes onto root.
type Transaction struct {
	root     *State
	lock     *sync.Mutex
	snapshot *State
	state    *State
	closed   bool
}

// newTransaction builds a Transaction snapshotting root under lock. The caller
// (engine.Begin) already holds nothing; Python's MemoryTransaction snapshots
// eagerly in __post_init__ without the lock, matching this constructor.
func newTransaction(root *State, lock *sync.Mutex) *Transaction {
	return &Transaction{
		root:     root,
		lock:     lock,
		snapshot: root.copyTables(),
		state:    root.copyTables(),
	}
}

// Commit persists this transaction's changes, or fails with a conflict error if
// a key it wrote was changed by a concurrently committed transaction. Idempotent
// after close.
func (t *Transaction) Commit(_ context.Context) error {
	if t.closed {
		return nil
	}
	t.lock.Lock()
	defer t.lock.Unlock()

	// Detect across all tables FIRST so a conflict aborts before any partial
	// merge is applied.
	conflict := detectConflict(t.root.SessionTokens, t.snapshot.SessionTokens, t.state.SessionTokens) ||
		detectConflict(t.root.ResumeTokens, t.snapshot.ResumeTokens, t.state.ResumeTokens) ||
		detectConflict(t.root.Sessions, t.snapshot.Sessions, t.state.Sessions) ||
		detectConflict(t.root.Approvals, t.snapshot.Approvals, t.state.Approvals) ||
		detectConflict(t.root.Leases, t.snapshot.Leases, t.state.Leases) ||
		detectConflict(t.root.GraphicalTargets, t.snapshot.GraphicalTargets, t.state.GraphicalTargets)
	if conflict {
		t.closed = true
		return cp.ConflictError("memory control-plane transaction conflicts with a concurrent commit")
	}
	mergeTable(t.root.SessionTokens, t.snapshot.SessionTokens, t.state.SessionTokens)
	mergeTable(t.root.ResumeTokens, t.snapshot.ResumeTokens, t.state.ResumeTokens)
	mergeTable(t.root.Sessions, t.snapshot.Sessions, t.state.Sessions)
	mergeTable(t.root.Approvals, t.snapshot.Approvals, t.state.Approvals)
	mergeTable(t.root.Leases, t.snapshot.Leases, t.state.Leases)
	mergeTable(t.root.GraphicalTargets, t.snapshot.GraphicalTargets, t.state.GraphicalTargets)
	t.closed = true
	return nil
}

// Rollback discards this transaction's working copy.
func (t *Transaction) Rollback(_ context.Context) error {
	t.closed = true
	return nil
}

// State exposes this transaction's working copy. It exists so tests can mirror
// the Python tests that reach into “tx.state“ (e.g. to drop a key and assert
// the delete branch of the merge). Production code goes through the stores.
func (t *Transaction) State() *State { return t.state }
