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

func TestResolveDatabasePathVariants(t *testing.T) {
	t.Parallel()
	tests := []struct {
		url, want string
	}{
		{":memory:", ":memory:"},
		{"file::memory:", ":memory:"},
		{"sqlite://:memory:", ":memory:"},
		{"/tmp/plain/path.db", "/tmp/plain/path.db"},
		{"sqlite:///tmp/abs/db.sqlite", "/tmp/abs/db.sqlite"},
		{"sqlite+aiosqlite:///x/y.db", "/x/y.db"},
	}
	for _, tc := range tests {
		t.Run(tc.url, func(t *testing.T) {
			t.Parallel()
			if got := sqlite.ResolveDatabasePath(tc.url); got != tc.want {
				t.Fatalf("ResolveDatabasePath(%q) = %q, want %q", tc.url, got, tc.want)
			}
		})
	}
}

func TestOpenCreatesParentDir(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	nested := filepath.Join(dir, "nested", "deep", "cp.db")
	e := sqlite.New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: nested})
	ctx := context.Background()
	if err := e.Open(ctx); err != nil {
		t.Fatalf("open: %v", err)
	}
	// A second Open is a no-op (idempotent).
	if err := e.Open(ctx); err != nil {
		t.Fatalf("second open: %v", err)
	}
	if err := e.Migrate(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	_ = e.Close(ctx)
	// A second Close is a no-op.
	if err := e.Close(ctx); err != nil {
		t.Fatalf("second close: %v", err)
	}
}

func TestOpenFailsWhenPathIsADirectory(t *testing.T) {
	t.Parallel()
	// A directory cannot be opened as a SQLite file: db.Conn fails, which the
	// connection layer reports as a ConnectionError.
	dir := t.TempDir()
	e := sqlite.New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: dir})
	if err := e.Open(context.Background()); err == nil {
		t.Fatal("expected Open to fail when the path is a directory")
	}
}

func TestMemoryURLSkipsWAL(t *testing.T) {
	t.Parallel()
	e := sqlite.New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: ":memory:"})
	ctx := context.Background()
	if err := e.Migrate(ctx); err != nil {
		t.Fatalf("migrate in-memory: %v", err)
	}
	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatalf("begin in-memory: %v", err)
	}
	rec := cp.SessionRecord{
		SessionID: "s1", DisplayName: "n", ConnectorType: "pty", Visibility: "private", LifecycleState: "waiting",
	}
	if err := e.SessionStore(tx).Upsert(ctx, rec); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	_ = e.Close(ctx)
}

func TestBeginReleasesLockAcrossTransactions(t *testing.T) {
	t.Parallel()
	e, _ := newPlaneWithPath(t)
	ctx := context.Background()
	// Commit and rollback both release the tx-lock; otherwise the next Begin
	// would block forever.
	tx, _ := e.Begin(ctx)
	_ = tx.Rollback(ctx)
	tx2, _ := e.Begin(ctx)
	_ = tx2.Commit(ctx)
	tx3, _ := e.Begin(ctx)
	_ = tx3.Rollback(ctx)
}

func TestTransactionIdempotent(t *testing.T) {
	t.Parallel()
	e, _ := newPlaneWithPath(t)
	ctx := context.Background()
	tx, _ := e.Begin(ctx)
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	// Second commit hits the closed early-exit.
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	tx2, _ := e.Begin(ctx)
	if err := tx2.Rollback(ctx); err != nil {
		t.Fatal(err)
	}
	// Second rollback hits the closed early-exit.
	if err := tx2.Rollback(ctx); err != nil {
		t.Fatal(err)
	}
}
