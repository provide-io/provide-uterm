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

// gtRecord builds a minimal graphical-target record.
func gtRecord(targetID string) cp.GraphicalTargetRecord {
	return cp.GraphicalTargetRecord{
		TargetID:    targetID,
		TenantID:    "acme",
		DisplayName: "console",
		Protocol:    "rfb",
		Width:       640,
		Height:      480,
		Config:      `{"vm_name":"vm-1"}`,
		CreatedAt:   100,
	}
}

// TestGraphicalTargetStoreRoundTrip covers put/get/list/delete against the
// memory backend directly. The registry-level tests in the graphical package
// exercise this code too, but Go attributes coverage per package, so those runs
// leave this store measured at 0% — it needs its own test here.
func TestGraphicalTargetStoreRoundTrip(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	e := memory.New(cp.Config{DatabaseURL: ":memory:"})
	if err := e.Open(ctx); err != nil {
		t.Fatalf("open: %v", err)
	}
	defer func() { _ = e.Close(ctx) }()

	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	store := e.GraphicalTargetStore(tx)

	if err := store.PutGraphicalTarget(ctx, gtRecord("gt-1")); err != nil {
		t.Fatalf("put: %v", err)
	}
	got, err := store.GetGraphicalTarget(ctx, "gt-1")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got == nil || got.TargetID != "gt-1" || got.TenantID != "acme" {
		t.Fatalf("get mismatch: %+v", got)
	}
	if miss, _ := store.GetGraphicalTarget(ctx, "missing"); miss != nil {
		t.Fatal("missing target should be nil")
	}

	removed, err := store.DeleteGraphicalTarget(ctx, "gt-1")
	if err != nil {
		t.Fatalf("delete: %v", err)
	}
	if !removed {
		t.Fatal("delete should report a removal")
	}
	if again, _ := store.DeleteGraphicalTarget(ctx, "gt-1"); again {
		t.Fatal("second delete should report nothing removed")
	}
	_ = tx.Rollback(ctx)
}

// TestGraphicalTargetStoreListIsSorted pins the ordering contract: Go map
// iteration is randomized, so without the explicit sort this backend would
// disagree with the SQLite one (which gets its order from ORDER BY).
func TestGraphicalTargetStoreListIsSorted(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	e := memory.New(cp.Config{DatabaseURL: ":memory:"})
	if err := e.Open(ctx); err != nil {
		t.Fatalf("open: %v", err)
	}
	defer func() { _ = e.Close(ctx) }()

	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	store := e.GraphicalTargetStore(tx)
	for _, id := range []string{"gt-c", "gt-a", "gt-b"} {
		if err := store.PutGraphicalTarget(ctx, gtRecord(id)); err != nil {
			t.Fatalf("put %s: %v", id, err)
		}
	}

	rows, err := store.ListGraphicalTargets(ctx)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(rows) != 3 {
		t.Fatalf("list len = %d, want 3", len(rows))
	}
	if rows[0].TargetID != "gt-a" || rows[1].TargetID != "gt-b" || rows[2].TargetID != "gt-c" {
		t.Fatalf("order = %s %s %s", rows[0].TargetID, rows[1].TargetID, rows[2].TargetID)
	}

	// An empty store lists empty rather than nil-panicking.
	_ = tx.Rollback(ctx)
	tx2, _ := e.Begin(ctx)
	empty, err := e.GraphicalTargetStore(tx2).ListGraphicalTargets(ctx)
	if err != nil {
		t.Fatalf("list empty: %v", err)
	}
	if len(empty) != 0 {
		t.Fatalf("expected empty listing, got %d", len(empty))
	}
	_ = tx2.Rollback(ctx)
}

// TestGraphicalTargetStoreCommitPersistsAcrossTransactions checks the write
// actually merges into shared state on commit, not just the tx-local copy.
func TestGraphicalTargetStoreCommitPersistsAcrossTransactions(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	e := memory.New(cp.Config{DatabaseURL: ":memory:"})
	if err := e.Open(ctx); err != nil {
		t.Fatalf("open: %v", err)
	}
	defer func() { _ = e.Close(ctx) }()

	tx, _ := e.Begin(ctx)
	if err := e.GraphicalTargetStore(tx).PutGraphicalTarget(ctx, gtRecord("gt-1")); err != nil {
		t.Fatalf("put: %v", err)
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatalf("commit: %v", err)
	}

	tx2, _ := e.Begin(ctx)
	got, err := e.GraphicalTargetStore(tx2).GetGraphicalTarget(ctx, "gt-1")
	if err != nil {
		t.Fatalf("get after commit: %v", err)
	}
	if got == nil {
		t.Fatal("committed target should be visible to a later transaction")
	}
	_ = tx2.Rollback(ctx)

	// A rolled-back write must NOT be visible.
	tx3, _ := e.Begin(ctx)
	_ = e.GraphicalTargetStore(tx3).PutGraphicalTarget(ctx, gtRecord("gt-2"))
	_ = tx3.Rollback(ctx)

	tx4, _ := e.Begin(ctx)
	if ghost, _ := e.GraphicalTargetStore(tx4).GetGraphicalTarget(ctx, "gt-2"); ghost != nil {
		t.Fatal("rolled-back target must not be visible")
	}
	_ = tx4.Rollback(ctx)
}
