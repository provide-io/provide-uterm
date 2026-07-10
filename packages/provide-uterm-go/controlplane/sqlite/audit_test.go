//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite_test

import (
	"context"
	"path/filepath"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/sqlite"
)

func TestAuditHeadFreshIsNil(t *testing.T) {
	t.Parallel()
	e, _ := newPlaneWithPath(t)
	if h, _ := e.GetAuditHead(context.Background()); h != nil {
		t.Fatalf("fresh head = %+v, want nil", h)
	}
}

func TestAuditHeadSetGetAndMonotonic(t *testing.T) {
	t.Parallel()
	e, _ := newPlaneWithPath(t)
	ctx := context.Background()
	_ = e.SetAuditHead(ctx, 1, "aa")
	if h, _ := e.GetAuditHead(ctx); h == nil || *h != (cp.AuditHead{Seq: 1, RecordHash: "aa"}) {
		t.Fatalf("set/get mismatch: %+v", h)
	}
	_ = e.SetAuditHead(ctx, 2, "bb")
	_ = e.SetAuditHead(ctx, 1, "zz") // lower: no-op
	_ = e.SetAuditHead(ctx, 2, "zz") // equal: no-op
	if h, _ := e.GetAuditHead(ctx); *h != (cp.AuditHead{Seq: 2, RecordHash: "bb"}) {
		t.Fatalf("monotonic guard failed: %+v", h)
	}
	_ = e.SetAuditHead(ctx, 3, "cc") // greater: advances
	if h, _ := e.GetAuditHead(ctx); *h != (cp.AuditHead{Seq: 3, RecordHash: "cc"}) {
		t.Fatalf("advance failed: %+v", h)
	}
}

func TestAuditHeadSurvivesReopen(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "cp.db")
	ctx := context.Background()
	e := sqlite.New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: path})
	e.SetClock(func() float64 { return 0 })
	if err := e.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	_ = e.SetAuditHead(ctx, 7, "deadbeef")
	_ = e.Close(ctx)

	reopened := sqlite.New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: path})
	reopened.SetClock(func() float64 { return 0 })
	if err := reopened.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	if h, _ := reopened.GetAuditHead(ctx); h == nil || *h != (cp.AuditHead{Seq: 7, RecordHash: "deadbeef"}) {
		t.Fatalf("head not durable across reopen: %+v", h)
	}
	// Monotonic guard persists across reopen too.
	_ = reopened.SetAuditHead(ctx, 3, "zz")
	if h, _ := reopened.GetAuditHead(ctx); *h != (cp.AuditHead{Seq: 7, RecordHash: "deadbeef"}) {
		t.Fatalf("guard lost across reopen: %+v", h)
	}
	_ = reopened.Close(ctx)
}
