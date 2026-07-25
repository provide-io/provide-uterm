//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite_test

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/sqlite"
)

func schemaVersions(t *testing.T, path string) []int {
	t.Helper()
	db := openRaw(t, path)
	rows, err := db.Query("SELECT version FROM cp_schema_version ORDER BY version")
	if err != nil {
		t.Fatalf("versions: %v", err)
	}
	defer func() { _ = rows.Close() }()
	var out []int
	for rows.Next() {
		var v int
		_ = rows.Scan(&v)
		out = append(out, v)
	}
	return out
}

func TestMigrateBootstrapsSchema(t *testing.T) {
	t.Parallel()
	_, path := newPlaneWithPath(t)
	tables := tableNames(t, path)
	for _, want := range []string{
		"cp_schema_version", "cp_sessions", "cp_session_tokens", "cp_resume_tokens",
		"cp_approvals", "cp_leases", "cp_audit_head", "cp_graphical_targets",
	} {
		if !tables[want] {
			t.Fatalf("missing table %q", want)
		}
	}
	if got := schemaVersions(t, path); len(got) != 3 || got[0] != 1 || got[1] != 2 || got[2] != 3 {
		t.Fatalf("versions = %v, want [1 2 3]", got)
	}
}

func TestMigrateIsIdempotent(t *testing.T) {
	t.Parallel()
	e, path := newPlaneWithPath(t)
	// newPlaneWithPath already migrated once; a second migrate must skip cleanly.
	if err := e.Migrate(context.Background()); err != nil {
		t.Fatalf("second migrate: %v", err)
	}
	if got := schemaVersions(t, path); len(got) != 3 {
		t.Fatalf("versions after re-migrate = %v, want [1 2 3]", got)
	}
}

func TestMigrateRejectsInvalidDatabaseFile(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "cp.db")
	if err := os.WriteFile(path, []byte("not a sqlite database"), 0o600); err != nil {
		t.Fatal(err)
	}
	e := sqlite.New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: path})
	err := e.Migrate(context.Background())
	var mErr *sqlite.MigrationError
	if !errors.As(err, &mErr) {
		t.Fatalf("expected MigrationError, got %v", err)
	}
	_ = e.Close(context.Background())
}

func TestMigrateWrapsConnectionError(t *testing.T) {
	t.Parallel()
	// Make the parent path un-creatable: a regular file stands where a directory
	// is needed, so MkdirAll fails inside connect -> ConnectionError, which
	// Migrate must wrap as a MigrationError.
	blocker := filepath.Join(t.TempDir(), "blocker")
	if err := os.WriteFile(blocker, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	e := sqlite.New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: filepath.Join(blocker, "sub", "cp.db")})
	err := e.Migrate(context.Background())
	var mErr *sqlite.MigrationError
	if !errors.As(err, &mErr) {
		t.Fatalf("expected wrapped MigrationError, got %T %v", err, err)
	}
}

func TestMigrateV1ForwardToV2(t *testing.T) {
	t.Parallel()
	// Hand-build a db already at schema version 1 (no audit-head table), then
	// re-migrate via the normal path: v1 is skipped, v2 is applied.
	path := filepath.Join(t.TempDir(), "cp.db")
	raw := openRaw(t, path)
	if _, err := raw.Exec(
		"CREATE TABLE cp_schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("INSERT INTO cp_schema_version(version, applied_at) VALUES(1, 0.0)"); err != nil {
		t.Fatal(err)
	}
	_ = raw.Close()

	e := sqlite.New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: path})
	e.SetClock(func() float64 { return 0 })
	if err := e.Migrate(context.Background()); err != nil {
		t.Fatalf("migrate v1->v2: %v", err)
	}
	if err := e.SetAuditHead(context.Background(), 5, "ff"); err != nil {
		t.Fatal(err)
	}
	head, _ := e.GetAuditHead(context.Background())
	if head == nil || *head != (cp.AuditHead{Seq: 5, RecordHash: "ff"}) {
		t.Fatalf("audit head after forward-migrate = %+v", head)
	}
	_ = e.Close(context.Background())
	if got := schemaVersions(t, path); len(got) != 3 || got[0] != 1 || got[1] != 2 || got[2] != 3 {
		t.Fatalf("versions = %v, want [1 2 3]", got)
	}
	names := tableNames(t, path)
	if !names["cp_audit_head"] {
		t.Fatal("cp_audit_head should exist after forward-migrate")
	}
	if !names["cp_graphical_targets"] {
		t.Fatal("cp_graphical_targets should exist after forward-migrate")
	}
}
