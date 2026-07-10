//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite

import (
	"context"
	"database/sql"
)

// txLock is a mutual-exclusion lock that also answers Locked(), reproducing the
// asyncio.Lock semantics the Python engine relies on (its _release_lock closure
// guards a double-release with “if self._tx_lock.locked()“). A buffered
// channel of capacity one is the lock; len() answers Locked().
type txLock struct{ ch chan struct{} }

func newTxLock() *txLock { return &txLock{ch: make(chan struct{}, 1)} }

// Lock blocks until the lock is acquired.
func (l *txLock) Lock() { l.ch <- struct{}{} }

// Unlock releases the lock.
func (l *txLock) Unlock() { <-l.ch }

// Locked reports whether the lock is currently held.
func (l *txLock) Locked() bool { return len(l.ch) == 1 }

// Transaction wraps the engine's dedicated *sql.Conn for the duration of a
// BEGIN IMMEDIATE...COMMIT/ROLLBACK span. Port of control.plane.sqlite.
// transaction.SqliteTransaction. commit/rollback are idempotent and, on first
// close, invoke onClose to release the engine tx-lock.
type Transaction struct {
	conn    *sql.Conn
	onClose func(ctx context.Context) // releases the engine tx-lock; may be nil
	closed  bool
}

// newTransaction builds a Transaction over conn. onClose may be nil (mirroring
// the Python SqliteTransaction, which accepts on_close=None).
func newTransaction(conn *sql.Conn, onClose func(ctx context.Context)) *Transaction {
	return &Transaction{conn: conn, onClose: onClose}
}

// Conn exposes the underlying connection to the stores bound to this tx.
func (t *Transaction) Conn() *sql.Conn { return t.conn }

// Commit commits the underlying transaction and releases the tx-lock.
func (t *Transaction) Commit(ctx context.Context) error {
	if t.closed {
		return nil
	}
	_, err := t.conn.ExecContext(ctx, "COMMIT")
	t.closed = true
	if t.onClose != nil {
		t.onClose(ctx)
	}
	return err
}

// Rollback rolls back the underlying transaction and releases the tx-lock.
func (t *Transaction) Rollback(ctx context.Context) error {
	if t.closed {
		return nil
	}
	_, err := t.conn.ExecContext(ctx, "ROLLBACK")
	t.closed = true
	if t.onClose != nil {
		t.onClose(ctx)
	}
	return err
}
