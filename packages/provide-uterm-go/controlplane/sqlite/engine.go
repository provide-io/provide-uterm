//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
)

// schemaVersionTable is the default migration bookkeeping table.
const schemaVersionTable = "cp_schema_version"

// Engine is the durable SQLite-backed control-plane backend. Port of
// control.plane.sqlite.engine.SqliteControlPlane.
type Engine struct {
	config cp.Config
	caps   cp.EngineCapabilities
	conn   *conn
	txLock *txLock

	// now supplies wall-clock timestamps for internal columns (migration
	// applied_at, audit-head updated_at, lease clear deleted_at). Injectable for
	// deterministic tests; defaults to time.Now unix seconds.
	now func() float64

	// consumeUpdateHook, when non-nil, runs just before ConsumeResumeToken's
	// UPDATE. Tests use it to revoke the row so the UPDATE matches zero rows,
	// deterministically exercising the rowcount!=1 branch (the Python test
	// monkeypatches _conn.execute for the same purpose).
	consumeUpdateHook func(ctx context.Context, tokenValue string)
}

// New builds a SQLite Engine from config. Defaults are applied so a Config with
// only DatabaseURL set still yields the Python defaults.
func New(config cp.Config) *Engine {
	config = config.Normalized()
	return &Engine{
		config: config,
		caps:   config.Capabilities,
		txLock: newTxLock(),
		now:    func() float64 { return float64(time.Now().UnixNano()) / 1e9 },
	}
}

// SetClock overrides the internal wall-clock source (test seam).
func (e *Engine) SetClock(now func() float64) { e.now = now }

// Capabilities returns the engine feature flags.
func (e *Engine) Capabilities() cp.EngineCapabilities { return e.caps }

// Open lazily establishes the SQLite connection (idempotent).
func (e *Engine) Open(ctx context.Context) error {
	if e.conn != nil {
		return nil
	}
	c, err := connectSQLite(ctx, e.config.DatabaseURL, 5000, true)
	if err != nil {
		return err
	}
	e.conn = c
	return nil
}

// Close releases the connection (idempotent).
func (e *Engine) Close(context.Context) error {
	if e.conn == nil {
		return nil
	}
	err := e.conn.close()
	e.conn = nil
	return err
}

// Migrate opens the connection and applies migrations under the tx-lock. Port of
// SqliteControlPlane.migrate: a MigrationError passes through; a ConnectionError
// is wrapped as a MigrationError.
func (e *Engine) Migrate(ctx context.Context) error {
	if err := e.Open(ctx); err != nil {
		// Open only ever fails with a *ConnectionError; wrap it as a
		// MigrationError to match the Python except SqliteConnectionError arm.
		return &MigrationError{msg: fmt.Sprintf("failed to apply control-plane migration: %v", err)}
	}
	e.txLock.Lock()
	defer e.txLock.Unlock()
	return applyMigrations(ctx, e.conn.conn, schemaVersionTable, e.now())
}

// Begin acquires the tx-lock and issues BEGIN IMMEDIATE. On failure the lock is
// released before returning. Port of SqliteControlPlane.begin.
func (e *Engine) Begin(ctx context.Context) (cp.Tx, error) {
	if err := e.Open(ctx); err != nil {
		return nil, err
	}
	e.txLock.Lock()
	if _, err := e.conn.conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		e.txLock.Unlock()
		return nil, err
	}
	release := func(context.Context) {
		if e.txLock.Locked() {
			e.txLock.Unlock()
		}
	}
	return newTransaction(e.conn.conn, release), nil
}

// Reap physically deletes rows past the retention cutoff, then truncates the
// WAL. Port of SqliteControlPlane.reap.
func (e *Engine) Reap(ctx context.Context, now float64, retentionS int) (int, error) {
	cutoff := now - float64(retentionS)
	tx, err := e.Begin(ctx)
	if err != nil {
		return 0, err
	}
	deleted, derr := e.runReapDeletes(ctx, cutoff)
	if derr != nil {
		_ = tx.Rollback(ctx)
		return 0, derr
	}
	if cerr := tx.Commit(ctx); cerr != nil {
		return 0, cerr
	}
	// The WAL checkpoint must run OUTSIDE the BEGIN IMMEDIATE txn.
	e.txLock.Lock()
	_, _ = e.conn.conn.ExecContext(ctx, "PRAGMA wal_checkpoint(TRUNCATE)")
	e.txLock.Unlock()
	return deleted, nil
}

// reapStatements are the five DELETEs, in the same order as the Python engine.
var reapStatements = []struct {
	sql    string
	params func(cutoff float64) []any
}{
	{
		sql:    "DELETE FROM cp_resume_tokens WHERE (revoked_at IS NOT NULL AND revoked_at < ?) OR expires_at < ?",
		params: func(c float64) []any { return []any{c, c} },
	},
	{
		sql: "DELETE FROM cp_session_tokens " +
			"WHERE (revoked_at IS NOT NULL AND revoked_at < ?) " +
			"OR (expires_at IS NOT NULL AND expires_at < ?)",
		params: func(c float64) []any { return []any{c, c} },
	},
	{
		sql:    "DELETE FROM cp_sessions WHERE deleted_at IS NOT NULL AND deleted_at < ?",
		params: func(c float64) []any { return []any{c} },
	},
	{
		sql:    "DELETE FROM cp_leases WHERE (deleted_at IS NOT NULL AND deleted_at < ?) OR lease_expires_at < ?",
		params: func(c float64) []any { return []any{c, c} },
	},
	{
		sql:    "DELETE FROM cp_approvals WHERE resolved_at IS NOT NULL AND resolved_at < ?",
		params: func(c float64) []any { return []any{c} },
	},
}

// runReapDeletes executes the reap DELETEs and sums the affected rows.
func (e *Engine) runReapDeletes(ctx context.Context, cutoff float64) (int, error) {
	deleted := 0
	for _, stmt := range reapStatements {
		res, err := e.conn.conn.ExecContext(ctx, stmt.sql, stmt.params(cutoff)...)
		if err != nil {
			return 0, err
		}
		n, err := res.RowsAffected()
		if err != nil {
			return 0, err
		}
		deleted += int(n)
	}
	return deleted, nil
}

// GetAuditHead returns the persisted audit-chain head, or nil at genesis. Port
// of SqliteControlPlane.get_audit_head.
func (e *Engine) GetAuditHead(ctx context.Context) (*cp.AuditHead, error) {
	if err := e.Open(ctx); err != nil {
		return nil, err
	}
	e.txLock.Lock()
	defer e.txLock.Unlock()
	row := e.conn.conn.QueryRowContext(ctx, "SELECT seq, record_hash FROM cp_audit_head WHERE id = 1")
	var head cp.AuditHead
	if err := row.Scan(&head.Seq, &head.RecordHash); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, nil
		}
		return nil, err
	}
	return &head, nil
}

// SetAuditHead persists the head monotonically in its own transaction. Port of
// SqliteControlPlane.set_audit_head: the WHERE excluded.seq > cp_audit_head.seq
// clause makes a lower-or-equal seq a no-op.
func (e *Engine) SetAuditHead(ctx context.Context, seq int64, recordHash string) error {
	tx, err := e.Begin(ctx)
	if err != nil {
		return err
	}
	_, execErr := e.conn.conn.ExecContext(ctx,
		"INSERT INTO cp_audit_head(id, seq, record_hash, updated_at) VALUES (1, ?, ?, ?) "+
			"ON CONFLICT(id) DO UPDATE SET "+
			"seq = excluded.seq, record_hash = excluded.record_hash, updated_at = excluded.updated_at "+
			"WHERE excluded.seq > cp_audit_head.seq",
		seq, recordHash, e.now())
	if execErr != nil {
		_ = tx.Rollback(ctx)
		return execErr
	}
	return tx.Commit(ctx)
}

// txConn resolves a Tx to the SQLite connection, panicking on a foreign Tx.
func txConn(tx cp.Tx) *Transaction {
	st, ok := tx.(*Transaction)
	if !ok {
		panic("sqlite: transaction is not a *sqlite.Transaction")
	}
	return st
}

// SessionStore returns a session store bound to tx.
func (e *Engine) SessionStore(tx cp.Tx) cp.SessionStore {
	return &sessionStore{conn: txConn(tx).conn}
}

// TokenStore returns a token store bound to tx.
func (e *Engine) TokenStore(tx cp.Tx) cp.TokenStore {
	return &tokenStore{eng: e, conn: txConn(tx).conn}
}

// ApprovalStore returns an approval store bound to tx.
func (e *Engine) ApprovalStore(tx cp.Tx) cp.ApprovalStore {
	return &approvalStore{conn: txConn(tx).conn}
}

// LeaseStore returns a lease store bound to tx.
func (e *Engine) LeaseStore(tx cp.Tx) cp.LeaseStore {
	return &leaseStore{eng: e, conn: txConn(tx).conn}
}

// GraphicalTargetStore returns a graphical-target store bound to tx.
func (e *Engine) GraphicalTargetStore(tx cp.Tx) cp.GraphicalTargetStore {
	return &graphicalTargetStore{conn: txConn(tx).conn}
}
