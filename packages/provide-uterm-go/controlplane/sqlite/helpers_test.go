//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite_test

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/sqlite"
	_ "modernc.org/sqlite"
)

// newPlaneWithPath builds a migrated SQLite engine on a fresh temp file and
// returns both the engine and the file path (for raw inspection). A fixed clock
// keeps internal timestamps deterministic.
func newPlaneWithPath(t *testing.T) (*sqlite.Engine, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "cp.db")
	e := sqlite.New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: path})
	e.SetClock(func() float64 { return 0 })
	if err := e.Migrate(context.Background()); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	t.Cleanup(func() { _ = e.Close(context.Background()) })
	return e, path
}

// openRaw opens a second, independent read connection to the same DB file, used
// to assert persisted state the way the Python tests use a plain sqlite3
// connection.
func openRaw(t *testing.T, path string) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatalf("open raw: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

// countRows returns COUNT(*) for a table via a raw connection.
func countRows(t *testing.T, path, table string) int {
	t.Helper()
	db := openRaw(t, path)
	var n int
	if err := db.QueryRow("SELECT count(*) FROM " + table).Scan(&n); err != nil {
		t.Fatalf("count %s: %v", table, err)
	}
	return n
}

// survivors returns the set of values in col for a table via a raw connection.
func survivors(t *testing.T, path, table, col string) map[string]bool {
	t.Helper()
	db := openRaw(t, path)
	rows, err := db.Query("SELECT " + col + " FROM " + table)
	if err != nil {
		t.Fatalf("survivors %s.%s: %v", table, col, err)
	}
	defer func() { _ = rows.Close() }()
	out := map[string]bool{}
	for rows.Next() {
		var v string
		if err := rows.Scan(&v); err != nil {
			t.Fatalf("scan: %v", err)
		}
		out[v] = true
	}
	return out
}

// tableNames returns the set of table names in the DB.
func tableNames(t *testing.T, path string) map[string]bool {
	t.Helper()
	db := openRaw(t, path)
	rows, err := db.Query("SELECT name FROM sqlite_master WHERE type='table'")
	if err != nil {
		t.Fatalf("tables: %v", err)
	}
	defer func() { _ = rows.Close() }()
	out := map[string]bool{}
	for rows.Next() {
		var n string
		_ = rows.Scan(&n)
		out[n] = true
	}
	return out
}
