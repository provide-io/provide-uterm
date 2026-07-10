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

// foreignTx is a Tx that is not a *memory.Transaction, used to prove the
// store-factory panics on a foreign transaction.
type foreignTx struct{}

func (foreignTx) Commit(context.Context) error   { return nil }
func (foreignTx) Rollback(context.Context) error { return nil }

func TestStoreFactoryPanicsOnForeignTx(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	defer func() {
		if recover() == nil {
			t.Fatal("expected panic on foreign tx")
		}
	}()
	e.SessionStore(foreignTx{})
}

func TestListPendingOrdersByCreatedAtThenID(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()
	tx, _ := e.Begin(ctx)
	store := e.ApprovalStore(tx)
	mk := func(id string, createdAt float64) cp.ApprovalRecord {
		return cp.ApprovalRecord{
			ApprovalID: id, SessionID: "s1", Command: "ls", State: "pending", CreatedAt: createdAt,
		}
	}
	// Insert out of order, including a created_at tie broken by approval_id.
	_ = store.PutApproval(ctx, mk("c", 30.0))
	_ = store.PutApproval(ctx, mk("a", 10.0))
	_ = store.PutApproval(ctx, mk("b-second", 20.0))
	_ = store.PutApproval(ctx, mk("b-first", 20.0))
	// A resolved approval must not appear in the pending list.
	_ = store.PutApproval(ctx, cp.ApprovalRecord{
		ApprovalID: "z", SessionID: "s1", Command: "ls", State: "approved", CreatedAt: 5.0, ResolvedAt: cp.Float(6.0),
	})
	_ = tx.Commit(ctx)

	read, _ := e.Begin(ctx)
	pending, _ := e.ApprovalStore(read).ListPending(ctx)
	_ = read.Rollback(ctx)

	var ids []string
	for _, r := range pending {
		ids = append(ids, r.ApprovalID)
	}
	want := []string{"a", "b-first", "b-second", "c"}
	if len(ids) != len(want) {
		t.Fatalf("ids = %v, want %v", ids, want)
	}
	for i := range want {
		if ids[i] != want[i] {
			t.Fatalf("ids = %v, want %v", ids, want)
		}
	}
}

// TestSnapshotCopiesAllTables commits a row into every table so a later Begin's
// snapshot exercises every branch of copyTables.
func TestSnapshotCopiesAllTables(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()
	tx, _ := e.Begin(ctx)
	_ = e.SessionStore(tx).Upsert(ctx, sampleSession("s1"))
	_ = e.TokenStore(tx).PutSessionToken(ctx, cp.SessionTokenRecord{
		SessionID: "s1", TokenKind: "op", TokenValue: "v", CreatedAt: 1,
	})
	_ = e.TokenStore(tx).CreateResumeToken(ctx, cp.ResumeTokenRecord{
		TokenValue: "r1", SessionID: "s1", Role: "viewer", CreatedAt: 1, ExpiresAt: 2,
	})
	_ = e.ApprovalStore(tx).PutApproval(ctx, cp.ApprovalRecord{
		ApprovalID: "a1", SessionID: "s1", Command: "ls", State: "pending", CreatedAt: 1,
	})
	_ = e.LeaseStore(tx).PutLease(ctx, leaseFor("s1", "alice"))
	_ = tx.Commit(ctx)

	// This Begin snapshots a root that now has every table populated.
	tx2, _ := e.Begin(ctx)
	if got, _ := e.SessionStore(tx2).Get(ctx, "s1"); got == nil {
		t.Fatal("expected populated snapshot")
	}
	_ = tx2.Rollback(ctx)
}
