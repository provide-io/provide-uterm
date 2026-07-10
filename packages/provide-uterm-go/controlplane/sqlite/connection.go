//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite

import (
	"context"
	"database/sql"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	_ "modernc.org/sqlite" // pure-Go SQLite driver; registers "sqlite".
)

// ConnectionError is returned when a SQLite control-plane connection cannot be
// initialized. Port of control.plane.sqlite.connection.SqliteConnectionError.
type ConnectionError struct{ msg string }

func (e *ConnectionError) Error() string { return e.msg }

// ResolveDatabasePath resolves a SQLite database URL or filesystem path to a
// connectable path. Port of control.plane.sqlite.connection.resolve_database_path.
func ResolveDatabasePath(databaseURL string) string {
	if databaseURL == ":memory:" || databaseURL == "file::memory:" {
		return ":memory:"
	}
	parsed, err := url.Parse(databaseURL)
	if err == nil && (parsed.Scheme == "sqlite" || parsed.Scheme == "sqlite+aiosqlite") {
		path, uerr := url.PathUnescape(parsed.Path)
		if uerr != nil {
			path = parsed.Path
		}
		if path == "" || path == "/:memory:" || path == ":memory:" {
			return ":memory:"
		}
		return path
	}
	return databaseURL
}

// conn is a live connection wrapper holding the single dedicated *sql.Conn that
// mirrors Python's one aiosqlite connection. All engine and store I/O runs on
// this conn; serialization is provided by the engine's tx-lock.
type conn struct {
	db   *sql.DB
	conn *sql.Conn
}

// connectSQLite opens a SQLite connection with the baseline bootstrap pragmas
// applied. Port of control.plane.sqlite.connection.connect_sqlite.
func connectSQLite(ctx context.Context, databaseURL string, busyTimeoutMS int, wal bool) (*conn, error) {
	dbPath := ResolveDatabasePath(databaseURL)
	if dbPath != ":memory:" {
		if dir := filepath.Dir(expandUser(dbPath)); dir != "" {
			if err := os.MkdirAll(dir, 0o755); err != nil {
				return nil, &ConnectionError{msg: fmt.Sprintf(
					"failed to initialize sqlite control-plane connection: %v", err)}
			}
		}
	}
	// MaxOpenConns(1) + a single grabbed *sql.Conn reproduces Python's single
	// aiosqlite connection: BEGIN IMMEDIATE and its COMMIT run on the same conn.
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return nil, &ConnectionError{msg: fmt.Sprintf(
			"failed to initialize sqlite control-plane connection: %v", err)}
	}
	db.SetMaxOpenConns(1)
	c, err := db.Conn(ctx)
	if err != nil {
		_ = db.Close()
		return nil, &ConnectionError{msg: fmt.Sprintf(
			"failed to initialize sqlite control-plane connection: %v", err)}
	}
	if _, err := c.ExecContext(ctx, fmt.Sprintf("PRAGMA busy_timeout=%d", busyTimeoutMS)); err != nil {
		_ = c.Close()
		_ = db.Close()
		return nil, &ConnectionError{msg: fmt.Sprintf(
			"failed to initialize sqlite control-plane connection: %v", err)}
	}
	if wal && dbPath != ":memory:" {
		if _, err := c.ExecContext(ctx, "PRAGMA journal_mode=WAL"); err != nil {
			_ = c.Close()
			_ = db.Close()
			return nil, &ConnectionError{msg: fmt.Sprintf(
				"failed to initialize sqlite control-plane connection: %v", err)}
		}
	}
	return &conn{db: db, conn: c}, nil
}

// close releases the dedicated conn and the pool.
func (c *conn) close() error {
	err := c.conn.Close()
	if dbErr := c.db.Close(); dbErr != nil && err == nil {
		err = dbErr
	}
	return err
}

// expandUser expands a leading ~ to the user's home directory, mirroring
// Python's Path.expanduser() used before mkdir.
func expandUser(p string) string {
	if p == "~" || strings.HasPrefix(p, "~/") {
		if home, err := os.UserHomeDir(); err == nil {
			if p == "~" {
				return home
			}
			return filepath.Join(home, p[2:])
		}
	}
	return p
}
