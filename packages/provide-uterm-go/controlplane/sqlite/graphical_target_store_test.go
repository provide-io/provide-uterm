//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite_test

import (
	"context"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/sqlite"
)

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

// putGT commits one record through its own transaction.
func putGT(t *testing.T, e *sqlite.Engine, rec cp.GraphicalTargetRecord) {
	t.Helper()
	ctx := context.Background()
	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	if err := e.GraphicalTargetStore(tx).PutGraphicalTarget(ctx, rec); err != nil {
		t.Fatalf("put: %v", err)
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatalf("commit: %v", err)
	}
}

func TestGraphicalTargetPutGetRoundTrip(t *testing.T) {
	t.Parallel()
	e, _ := newPlaneWithPath(t)
	ctx := context.Background()
	putGT(t, e, gtRecord("gt-1"))

	tx, _ := e.Begin(ctx)
	got, err := e.GraphicalTargetStore(tx).GetGraphicalTarget(ctx, "gt-1")
	_ = tx.Rollback(ctx)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got == nil {
		t.Fatal("expected a record")
	}
	if got.TargetID != "gt-1" || got.TenantID != "acme" || got.Width != 640 {
		t.Fatalf("round-trip mismatch: %+v", got)
	}
	if got.Config != `{"vm_name":"vm-1"}` {
		t.Fatalf("config = %q", got.Config)
	}
}

func TestGraphicalTargetGetAbsentIsNil(t *testing.T) {
	t.Parallel()
	e, _ := newPlaneWithPath(t)
	ctx := context.Background()
	tx, _ := e.Begin(ctx)
	got, err := e.GraphicalTargetStore(tx).GetGraphicalTarget(ctx, "missing")
	_ = tx.Rollback(ctx)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got != nil {
		t.Fatalf("expected nil, got %+v", got)
	}
}

func TestGraphicalTargetPutIsUpsert(t *testing.T) {
	t.Parallel()
	e, _ := newPlaneWithPath(t)
	ctx := context.Background()

	putGT(t, e, gtRecord("gt-1"))
	second := gtRecord("gt-1")
	second.DisplayName = "renamed"
	second.UpdatedAt = cp.Float(200)
	second.UpdatedBy = cp.Str("ops")
	putGT(t, e, second)

	tx, _ := e.Begin(ctx)
	store := e.GraphicalTargetStore(tx)
	got, _ := store.GetGraphicalTarget(ctx, "gt-1")
	rows, _ := store.ListGraphicalTargets(ctx)
	_ = tx.Rollback(ctx)

	if got == nil || got.DisplayName != "renamed" {
		t.Fatalf("upsert did not replace: %+v", got)
	}
	if !got.UpdatedAt.Valid || got.UpdatedAt.Float64 != 200 || got.UpdatedBy.String != "ops" {
		t.Fatalf("update stamps not persisted: %+v", got)
	}
	if len(rows) != 1 {
		t.Fatalf("upsert inserted a second row: %d", len(rows))
	}
}

func TestGraphicalTargetListIsOrdered(t *testing.T) {
	t.Parallel()
	e, _ := newPlaneWithPath(t)
	ctx := context.Background()
	for _, id := range []string{"gt-c", "gt-a", "gt-b"} {
		putGT(t, e, gtRecord(id))
	}

	tx, _ := e.Begin(ctx)
	rows, err := e.GraphicalTargetStore(tx).ListGraphicalTargets(ctx)
	_ = tx.Rollback(ctx)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	got := []string{rows[0].TargetID, rows[1].TargetID, rows[2].TargetID}
	if got[0] != "gt-a" || got[1] != "gt-b" || got[2] != "gt-c" {
		t.Fatalf("order = %v, want [gt-a gt-b gt-c]", got)
	}
}

func TestGraphicalTargetDeleteReportsRemoval(t *testing.T) {
	t.Parallel()
	e, _ := newPlaneWithPath(t)
	ctx := context.Background()
	putGT(t, e, gtRecord("gt-1"))

	tx, _ := e.Begin(ctx)
	store := e.GraphicalTargetStore(tx)
	first, err := store.DeleteGraphicalTarget(ctx, "gt-1")
	if err != nil {
		t.Fatalf("delete: %v", err)
	}
	second, err := store.DeleteGraphicalTarget(ctx, "gt-1")
	if err != nil {
		t.Fatalf("second delete: %v", err)
	}
	_ = tx.Commit(ctx)

	if !first {
		t.Fatal("first delete should report a removal")
	}
	if second {
		t.Fatal("second delete should report nothing removed")
	}
}

// TestGraphicalTargetBooleansAndNullsRoundTrip covers the INTEGER 0/1 columns
// and the nullable TEXT columns together — the two places a column-order or
// type slip would show up.
func TestGraphicalTargetBooleansAndNullsRoundTrip(t *testing.T) {
	t.Parallel()
	e, _ := newPlaneWithPath(t)
	ctx := context.Background()

	rec := gtRecord("gt-1")
	rec.IsSystem = true
	rec.IsStatic = false
	rec.Endpoint = cp.Str("host:5900")
	rec.Secret = cp.NullStr()
	rec.CaSecretRef = cp.Str("env:CA")
	rec.ClientKeySecretRef = cp.NullStr()
	rec.CreatedBy = cp.Str("alice")
	putGT(t, e, rec)

	tx, _ := e.Begin(ctx)
	got, err := e.GraphicalTargetStore(tx).GetGraphicalTarget(ctx, "gt-1")
	_ = tx.Rollback(ctx)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if !got.IsSystem || got.IsStatic {
		t.Fatalf("booleans wrong: is_system=%v is_static=%v", got.IsSystem, got.IsStatic)
	}
	if got.Endpoint.String != "host:5900" || !got.Endpoint.Valid {
		t.Fatalf("endpoint = %+v", got.Endpoint)
	}
	if got.Secret.Valid || got.ClientKeySecretRef.Valid {
		t.Fatal("absent columns should scan back as NULL")
	}
	if got.CaSecretRef.String != "env:CA" || got.CreatedBy.String != "alice" {
		t.Fatalf("nullable strings wrong: %+v", got)
	}
}

// TestGraphicalTargetEmptyConfigSatisfiesNotNull guards the NOT NULL config
// column: the zero-value record must still write valid JSON.
func TestGraphicalTargetEmptyConfigSatisfiesNotNull(t *testing.T) {
	t.Parallel()
	e, _ := newPlaneWithPath(t)
	ctx := context.Background()

	rec := gtRecord("gt-1")
	rec.Config = ""
	putGT(t, e, rec)

	tx, _ := e.Begin(ctx)
	got, err := e.GraphicalTargetStore(tx).GetGraphicalTarget(ctx, "gt-1")
	_ = tx.Rollback(ctx)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got.Config != "{}" {
		t.Fatalf("config = %q, want {}", got.Config)
	}
}

func TestGraphicalTargetRollbackDiscards(t *testing.T) {
	t.Parallel()
	e, _ := newPlaneWithPath(t)
	ctx := context.Background()

	tx, _ := e.Begin(ctx)
	if err := e.GraphicalTargetStore(tx).PutGraphicalTarget(ctx, gtRecord("gt-1")); err != nil {
		t.Fatalf("put: %v", err)
	}
	_ = tx.Rollback(ctx)

	tx2, _ := e.Begin(ctx)
	got, _ := e.GraphicalTargetStore(tx2).GetGraphicalTarget(ctx, "gt-1")
	_ = tx2.Rollback(ctx)
	if got != nil {
		t.Fatalf("rollback did not discard: %+v", got)
	}
}
