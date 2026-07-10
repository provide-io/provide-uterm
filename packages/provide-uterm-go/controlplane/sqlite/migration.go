//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite

import (
	"context"
	"database/sql"
	"fmt"
)

// MigrationError is returned when the SQLite control-plane schema cannot be
// migrated. Port of control.plane.sqlite.migration.SqliteMigrationError.
type MigrationError struct{ msg string }

func (e *MigrationError) Error() string { return e.msg }

// isIdentifier reports whether s is a valid Python identifier, matching the
// guard str.isidentifier() applies to the migration table name. ASCII-only is
// sufficient for the fixed "cp_schema_version" default and the test inputs.
func isIdentifier(s string) bool {
	if s == "" {
		return false
	}
	for i, r := range s {
		isLetter := (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || r == '_'
		isDigit := r >= '0' && r <= '9'
		if i == 0 && !isLetter {
			return false
		}
		if i > 0 && !isLetter && !isDigit {
			return false
		}
	}
	return true
}

// applyMigrations applies the inert control-plane schema migrations in order.
// Port of control.plane.sqlite.migration.apply_migrations. now supplies the
// applied_at timestamp (injected for deterministic tests).
func applyMigrations(ctx context.Context, c *sql.Conn, migrationTable string, now float64) error {
	if !isIdentifier(migrationTable) {
		return &MigrationError{msg: fmt.Sprintf("invalid migration table name: %q", migrationTable)}
	}
	err := applyMigrationsInner(ctx, c, migrationTable, now)
	if err != nil {
		// Best-effort rollback of any autocommit-uncommitted work, then wrap.
		_, _ = c.ExecContext(ctx, "ROLLBACK")
		return &MigrationError{msg: fmt.Sprintf("failed to apply control-plane migration: %v", err)}
	}
	return nil
}

// applyMigrationsInner runs the create/version-scan/apply loop. Kept separate so
// applyMigrations can wrap any failure in a MigrationError, matching the Python
// try/except that converts every exception to SqliteMigrationError.
func applyMigrationsInner(ctx context.Context, c *sql.Conn, migrationTable string, now float64) error {
	if _, err := c.ExecContext(ctx, fmt.Sprintf(migrationTableCreate, migrationTable)); err != nil {
		return err
	}
	//nolint:gosec // migrationTable is validated by isIdentifier above.
	row := c.QueryRowContext(ctx, fmt.Sprintf("SELECT COALESCE(MAX(version), 0) FROM %s", migrationTable))
	var current int
	if err := row.Scan(&current); err != nil {
		return err
	}
	for _, m := range migrations {
		if m.version <= current {
			continue
		}
		if err := execScript(ctx, c, m.sql); err != nil {
			return err
		}
		//nolint:gosec // migrationTable is validated by isIdentifier above.
		insert := fmt.Sprintf("INSERT INTO %s(version, applied_at) VALUES(?, ?)", migrationTable)
		if _, err := c.ExecContext(ctx, insert, m.version, now); err != nil {
			return err
		}
	}
	return nil
}

// execScript runs a multi-statement SQL script. modernc.org/sqlite executes all
// statements in a single ExecContext, matching aiosqlite's executescript.
func execScript(ctx context.Context, c *sql.Conn, script string) error {
	_, err := c.ExecContext(ctx, script)
	return err
}
