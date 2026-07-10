//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
)

func migratedEngine(t *testing.T) (*Engine, context.Context) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "cp.db")
	e := New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: path})
	e.SetClock(func() float64 { return 0 })
	ctx := context.Background()
	if err := e.Migrate(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	t.Cleanup(func() { _ = e.Close(ctx) })
	return e, ctx
}

func TestIsIdentifier(t *testing.T) {
	t.Parallel()
	tests := []struct {
		in   string
		want bool
	}{
		{"", false},
		{"cp_schema_version", true},
		{"_leading_underscore", true},
		{"1starts_with_digit", false},
		{"has space", false},
		{"bad name!", false},
		{"ok123", true},
		{"has-dash", false},
	}
	for _, tc := range tests {
		if got := isIdentifier(tc.in); got != tc.want {
			t.Fatalf("isIdentifier(%q) = %v, want %v", tc.in, got, tc.want)
		}
	}
}

func TestApplyMigrationsRejectsBadTableName(t *testing.T) {
	t.Parallel()
	e, ctx := migratedEngine(t)
	err := applyMigrations(ctx, e.conn.conn, "bad name!", 0)
	var mErr *MigrationError
	if err == nil || !asMigration(err, &mErr) {
		t.Fatalf("expected MigrationError, got %v", err)
	}
	if mErr.Error() == "" {
		t.Fatal("MigrationError.Error() should be non-empty")
	}
}

func TestErrorMessages(t *testing.T) {
	t.Parallel()
	if (&MigrationError{msg: "m"}).Error() != "m" {
		t.Fatal("MigrationError.Error mismatch")
	}
	if (&ConnectionError{msg: "c"}).Error() != "c" {
		t.Fatal("ConnectionError.Error mismatch")
	}
}

func TestTransactionConnAccessor(t *testing.T) {
	t.Parallel()
	e, ctx := migratedEngine(t)
	tx, _ := e.Begin(ctx)
	st := tx.(*Transaction)
	if st.Conn() == nil {
		t.Fatal("Conn() should return the underlying connection")
	}
	_ = tx.Rollback(ctx)
}

func TestBeginReleasesLockWhenBeginImmediateFails(t *testing.T) {
	t.Parallel()
	e, ctx := migratedEngine(t)
	// Open a dangling transaction directly on the connection so the engine's
	// BEGIN IMMEDIATE errors ("cannot start a transaction within a transaction").
	if _, err := e.conn.conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		t.Fatal(err)
	}
	if _, err := e.Begin(ctx); err == nil {
		t.Fatal("expected Begin to fail with a nested-transaction error")
	}
	// Clean up the dangling transaction; the tx-lock must be free for the
	// following Begin to proceed.
	if _, err := e.conn.conn.ExecContext(ctx, "ROLLBACK"); err != nil {
		t.Fatal(err)
	}
	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatalf("lock was not released after Begin failure: %v", err)
	}
	_ = tx.Rollback(ctx)
}

func TestReleaseLockSkipsWhenAlreadyUnlocked(t *testing.T) {
	t.Parallel()
	e, ctx := migratedEngine(t)
	tx, _ := e.Begin(ctx)
	// Release the tx-lock out-of-band so the on-close closure sees it unlocked
	// and takes the Locked()==false branch (skipping a second Unlock).
	e.txLock.Unlock()
	if err := tx.Commit(ctx); err != nil {
		t.Fatalf("commit: %v", err)
	}
}

func TestBeginPropagatesOpenError(t *testing.T) {
	t.Parallel()
	blocker := filepath.Join(t.TempDir(), "blocker")
	if err := os.WriteFile(blocker, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	e := New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: filepath.Join(blocker, "sub", "cp.db")})
	if _, err := e.Begin(context.Background()); err == nil {
		t.Fatal("expected Begin to fail when the connection cannot open")
	}
}

func TestConsumeResumeTokenRowcountZero(t *testing.T) {
	t.Parallel()
	e, ctx := migratedEngine(t)
	rec := cp.ResumeTokenRecord{
		TokenValue: "rowcount-test", SessionID: "s1", Role: "admin", CreatedAt: 1.0, ExpiresAt: 9999.0,
	}
	tx0, _ := e.Begin(ctx)
	_ = e.TokenStore(tx0).CreateResumeToken(ctx, rec)
	_ = tx0.Commit(ctx)

	// Revoke the row between the consume SELECT and its UPDATE so the UPDATE
	// (WHERE revoked_at IS NULL) matches zero rows — the rowcount!=1 branch.
	e.consumeUpdateHook = func(hookCtx context.Context, tokenValue string) {
		_, _ = e.conn.conn.ExecContext(hookCtx,
			"UPDATE cp_resume_tokens SET revoked_at = 1 WHERE token_value = ?", tokenValue)
	}
	tx, _ := e.Begin(ctx)
	got, err := e.TokenStore(tx).ConsumeResumeToken(ctx, "rowcount-test", 2.0)
	_ = tx.Rollback(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if got != nil {
		t.Fatalf("expected nil when UPDATE matches zero rows, got %+v", got)
	}
}

func TestSetAuditHeadRollsBackOnError(t *testing.T) {
	t.Parallel()
	e, ctx := migratedEngine(t)
	// Drop the audit-head table so the upsert fails; SetAuditHead must roll back,
	// return the error, and free the tx-lock.
	if _, err := e.conn.conn.ExecContext(ctx, "DROP TABLE cp_audit_head"); err != nil {
		t.Fatal(err)
	}
	if err := e.SetAuditHead(ctx, 1, "aa"); err == nil {
		t.Fatal("expected SetAuditHead to fail with the table dropped")
	}
	// GetAuditHead now hits its non-NoRows scan-error branch (no such table).
	if _, err := e.GetAuditHead(ctx); err == nil {
		t.Fatal("expected GetAuditHead to fail with the table dropped")
	}
	// The lock must be free: a follow-up Begin must not deadlock.
	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatalf("lock not released after SetAuditHead failure: %v", err)
	}
	_ = tx.Rollback(ctx)
}

func TestReapRollsBackAndReleasesLockOnError(t *testing.T) {
	t.Parallel()
	e, ctx := migratedEngine(t)
	// Drop a table so the first reap DELETE fails; reap must roll back, return
	// the error, and free the tx-lock.
	if _, err := e.conn.conn.ExecContext(ctx, "DROP TABLE cp_resume_tokens"); err != nil {
		t.Fatal(err)
	}
	if _, err := e.Reap(ctx, 1000, 100); err == nil {
		t.Fatal("expected reap to fail with a dropped table")
	}
	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatalf("lock not released after reap failure: %v", err)
	}
	_ = tx.Rollback(ctx)
}

func TestStoreFactoriesPanicOnForeignTx(t *testing.T) {
	t.Parallel()
	e, _ := migratedEngine(t)
	defer func() {
		if recover() == nil {
			t.Fatal("expected panic on foreign tx")
		}
	}()
	e.SessionStore(foreignTx{})
}

type foreignTx struct{}

func (foreignTx) Commit(context.Context) error   { return nil }
func (foreignTx) Rollback(context.Context) error { return nil }

func TestExpandUser(t *testing.T) {
	t.Parallel()
	home, err := os.UserHomeDir()
	if err != nil {
		t.Skip("no home dir")
	}
	if got := expandUser("~"); got != home {
		t.Fatalf("expandUser(~) = %q, want %q", got, home)
	}
	if got := expandUser("~/x"); got != filepath.Join(home, "x") {
		t.Fatalf("expandUser(~/x) = %q", got)
	}
	if got := expandUser("/abs/path"); got != "/abs/path" {
		t.Fatalf("expandUser(/abs/path) = %q", got)
	}
}

func TestApplyMigrationsWrapsInnerErrorOnClosedConn(t *testing.T) {
	t.Parallel()
	e, ctx := migratedEngine(t)
	c := e.conn.conn
	_ = e.Close(ctx) // closes c; subsequent Exec on it fails.
	err := applyMigrations(ctx, c, "cp_schema_version", 0)
	var mErr *MigrationError
	if err == nil || !asMigration(err, &mErr) {
		t.Fatalf("expected wrapped MigrationError, got %v", err)
	}
}

func TestReapPropagatesBeginError(t *testing.T) {
	t.Parallel()
	e, ctx := migratedEngine(t)
	// A dangling transaction makes reap's internal BEGIN IMMEDIATE fail.
	if _, err := e.conn.conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		t.Fatal(err)
	}
	if _, err := e.Reap(ctx, 1000, 100); err == nil {
		t.Fatal("expected reap to fail when its Begin fails")
	}
	_, _ = e.conn.conn.ExecContext(ctx, "ROLLBACK")
}

func TestCommitAndRollbackSurfaceExecErrors(t *testing.T) {
	t.Parallel()
	// Commit path.
	e1, ctx := migratedEngine(t)
	tx1, _ := e1.Begin(ctx)
	_ = e1.conn.conn.Close() // close the dedicated conn so COMMIT errors.
	if err := tx1.Commit(ctx); err == nil {
		t.Fatal("expected Commit to surface the exec error")
	}

	// Rollback path.
	e2, _ := migratedEngine(t)
	tx2, _ := e2.Begin(ctx)
	_ = e2.conn.conn.Close()
	if err := tx2.Rollback(ctx); err == nil {
		t.Fatal("expected Rollback to surface the exec error")
	}
}

func TestListPendingSurfacesQueryError(t *testing.T) {
	t.Parallel()
	e, ctx := migratedEngine(t)
	if _, err := e.conn.conn.ExecContext(ctx, "DROP TABLE cp_approvals"); err != nil {
		t.Fatal(err)
	}
	tx, _ := e.Begin(ctx)
	if _, err := e.ApprovalStore(tx).ListPending(ctx); err == nil {
		t.Fatal("expected ListPending to fail with the table dropped")
	}
	_ = tx.Rollback(ctx)
}

func TestTransactionWithNilOnCloseIsIdempotent(t *testing.T) {
	t.Parallel()
	e, ctx := migratedEngine(t)
	// Construct a transaction with onClose=nil (the Python on_close=None case).
	// It bypasses the engine tx-lock, so drive BEGIN/COMMIT on the conn directly.
	if _, err := e.conn.conn.ExecContext(ctx, "BEGIN"); err != nil {
		t.Fatal(err)
	}
	tx := newTransaction(e.conn.conn, nil)
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(ctx); err != nil { // idempotent early-out
		t.Fatal(err)
	}
	if _, err := e.conn.conn.ExecContext(ctx, "BEGIN"); err != nil {
		t.Fatal(err)
	}
	tx2 := newTransaction(e.conn.conn, nil)
	if err := tx2.Rollback(ctx); err != nil {
		t.Fatal(err)
	}
	if err := tx2.Rollback(ctx); err != nil { // idempotent early-out
		t.Fatal(err)
	}
}

func TestAuditHeadOperationsPropagateOpenError(t *testing.T) {
	t.Parallel()
	blocker := filepath.Join(t.TempDir(), "blocker")
	if err := os.WriteFile(blocker, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	badURL := filepath.Join(blocker, "sub", "cp.db")
	ctx := context.Background()

	e1 := New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: badURL})
	if _, err := e1.GetAuditHead(ctx); err == nil {
		t.Fatal("expected GetAuditHead to fail when the connection cannot open")
	}
	e2 := New(cp.Config{Backend: cp.BackendSQLite, DatabaseURL: badURL})
	if err := e2.SetAuditHead(ctx, 1, "aa"); err == nil {
		t.Fatal("expected SetAuditHead to fail when the connection cannot open")
	}
}

// asMigration is a tiny errors.As shim kept local so the white-box file avoids
// importing errors just for one call site.
func asMigration(err error, target **MigrationError) bool {
	m, ok := err.(*MigrationError)
	if ok {
		*target = m
	}
	return ok
}
