//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
)

// The real driver only fails these calls on a genuine disk or driver fault, so
// the engine's handling of them is driven through a thin wrapper around that
// same driver which fails the statements a test arms, and nothing else. Every
// other statement runs against real SQLite, so the code under test sees a real
// database right up to the injected failure.

var errInjected = errors.New("injected driver fault")

// faults is the per-test arming state. A statement "matches" a rule when its
// SQL contains the rule's substring.
type faults struct {
	mu              sync.Mutex
	execErr         string // ExecContext fails before reaching SQLite
	queryErr        string // QueryContext fails before reaching SQLite
	rowsAffectedErr string // the statement runs; RowsAffected then fails
	rowsErr         string // the query runs; iteration then fails
	closeErr        bool   // closing the driver connection fails
}

func (f *faults) set(mutate func(*faults)) {
	f.mu.Lock()
	defer f.mu.Unlock()
	mutate(f)
}

func (f *faults) match(rule func(*faults) string, query string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	s := rule(f)
	return s != "" && strings.Contains(query, s)
}

var faultDriverSeq atomic.Int64

// faultEngine returns an un-migrated engine whose connection goes through a
// freshly registered fault-injecting driver, plus that driver's fault state.
func faultEngine(t *testing.T) (*Engine, *faults) {
	t.Helper()
	probe, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	inner := probe.Driver()
	_ = probe.Close()

	f := &faults{}
	name := fmt.Sprintf("sqlite-fault-%d", faultDriverSeq.Add(1))
	sql.Register(name, &faultDriver{inner: inner, f: f})

	e := New(cpConfigForPath(t))
	e.SetClock(func() float64 { return 0 })
	e.driverName = name
	t.Cleanup(func() { _ = e.Close(context.Background()) })
	return e, f
}

type faultDriver struct {
	inner driver.Driver
	f     *faults
}

func (d *faultDriver) Open(name string) (driver.Conn, error) {
	c, err := d.inner.Open(name)
	if err != nil {
		return nil, err
	}
	return &faultConn{Conn: c, f: d.f}, nil
}

type faultConn struct {
	driver.Conn
	f *faults
}

func (c *faultConn) ExecContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Result, error) {
	if c.f.match(func(f *faults) string { return f.execErr }, query) {
		return nil, errInjected
	}
	res, err := c.Conn.(driver.ExecerContext).ExecContext(ctx, query, args)
	if err == nil && c.f.match(func(f *faults) string { return f.rowsAffectedErr }, query) {
		return faultResult{res}, nil
	}
	return res, err
}

func (c *faultConn) QueryContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Rows, error) {
	if c.f.match(func(f *faults) string { return f.queryErr }, query) {
		return nil, errInjected
	}
	rows, err := c.Conn.(driver.QueryerContext).QueryContext(ctx, query, args)
	if err == nil && c.f.match(func(f *faults) string { return f.rowsErr }, query) {
		return faultRows{rows}, nil
	}
	return rows, err
}

func (c *faultConn) Close() error {
	err := c.Conn.Close()
	c.f.mu.Lock()
	defer c.f.mu.Unlock()
	if c.f.closeErr {
		return errInjected
	}
	return err
}

type faultResult struct{ driver.Result }

func (faultResult) RowsAffected() (int64, error) { return 0, errInjected }

// faultRows ends iteration with an error instead of io.EOF, the way a read
// failure part-way through a result set surfaces through rows.Err().
type faultRows struct{ driver.Rows }

func (faultRows) Next([]driver.Value) error { return errInjected }

func cpConfigForPath(t *testing.T) cp.Config {
	t.Helper()
	return cp.Config{Backend: cp.BackendSQLite, DatabaseURL: filepath.Join(t.TempDir(), "cp.db")}
}

func resumeRecord(token string) cp.ResumeTokenRecord {
	return cp.ResumeTokenRecord{TokenValue: token, SessionID: "s1", Role: "viewer", CreatedAt: 1, ExpiresAt: 1e12}
}

// migratedFaultEngine is faultEngine after a clean migration.
func migratedFaultEngine(t *testing.T) (*Engine, *faults, context.Context) {
	t.Helper()
	e, f := faultEngine(t)
	ctx := context.Background()
	if err := e.Migrate(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	return e, f, ctx
}

func TestConnectUnknownDriverIsConnectionError(t *testing.T) {
	t.Parallel()
	e := New(cpConfigForPath(t))
	e.driverName = "no-such-driver"
	err := e.Open(context.Background())
	var cErr *ConnectionError
	if !errors.As(err, &cErr) || !strings.Contains(err.Error(), "no-such-driver") {
		t.Fatalf("Open = %v, want a ConnectionError naming the driver", err)
	}
}

func TestConnectBusyTimeoutFailureIsConnectionError(t *testing.T) {
	t.Parallel()
	e, f := faultEngine(t)
	f.set(func(f *faults) { f.execErr = "PRAGMA busy_timeout" })
	err := e.Open(context.Background())
	var cErr *ConnectionError
	if !errors.As(err, &cErr) || !strings.Contains(err.Error(), errInjected.Error()) {
		t.Fatalf("Open = %v, want a ConnectionError carrying the pragma failure", err)
	}
	if e.conn != nil {
		t.Fatal("a failed Open must not leave a connection behind")
	}
}

func TestCloseReportsPoolCloseFailure(t *testing.T) {
	t.Parallel()
	e, f, ctx := migratedFaultEngine(t)
	f.set(func(f *faults) { f.closeErr = true })
	if err := e.Close(ctx); !errors.Is(err, errInjected) {
		t.Fatalf("Close = %v, want the driver close failure", err)
	}
	if e.conn != nil {
		t.Fatal("Close must drop the connection even when closing it failed")
	}
}

func TestMigrateSurfacesEachStepFailure(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name string
		arm  func(*faults)
	}{
		{"version scan", func(f *faults) { f.queryErr = "SELECT COALESCE(MAX(version), 0)" }},
		{"schema script", func(f *faults) { f.execErr = "CREATE TABLE IF NOT EXISTS cp_sessions" }},
		{"version insert", func(f *faults) { f.execErr = "INSERT INTO cp_schema_version" }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			e, f := faultEngine(t)
			f.set(tc.arm)
			err := e.Migrate(context.Background())
			var mErr *MigrationError
			if !errors.As(err, &mErr) || !strings.Contains(err.Error(), errInjected.Error()) {
				t.Fatalf("Migrate = %v, want a MigrationError carrying the fault", err)
			}
		})
	}
}

func TestReapCommitFailureIsReturned(t *testing.T) {
	t.Parallel()
	e, f, ctx := migratedFaultEngine(t)
	f.set(func(f *faults) { f.execErr = "COMMIT" })
	n, err := e.Reap(ctx, 1000, 10)
	if !errors.Is(err, errInjected) || n != 0 {
		t.Fatalf("Reap = %d, %v; want 0 and the commit failure", n, err)
	}
	if e.txLock.Locked() {
		t.Fatal("a failed commit must still release the tx-lock")
	}
}

func TestReapRowsAffectedFailureIsReturned(t *testing.T) {
	t.Parallel()
	e, f, ctx := migratedFaultEngine(t)
	f.set(func(f *faults) { f.rowsAffectedErr = "DELETE FROM cp_resume_tokens" })
	n, err := e.Reap(ctx, 1000, 10)
	if !errors.Is(err, errInjected) || n != 0 {
		t.Fatalf("Reap = %d, %v; want 0 and the RowsAffected failure", n, err)
	}
	if e.txLock.Locked() {
		t.Fatal("a failed reap must roll back and release the tx-lock")
	}
}

func TestListPendingIterationFailureIsReturned(t *testing.T) {
	t.Parallel()
	e, f, ctx := migratedFaultEngine(t)
	f.set(func(f *faults) { f.rowsErr = "FROM cp_approvals WHERE state = 'pending'" })
	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	got, err := e.ApprovalStore(tx).ListPending(ctx)
	if !errors.Is(err, errInjected) || got != nil {
		t.Fatalf("ListPending = %v, %v; want nil and the iteration failure", got, err)
	}
}

func TestGraphicalTargetListIterationFailureIsReturned(t *testing.T) {
	t.Parallel()
	e, f, ctx := migratedFaultEngine(t)
	f.set(func(f *faults) { f.rowsErr = "FROM cp_graphical_targets ORDER BY target_id" })
	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	got, err := e.GraphicalTargetStore(tx).ListGraphicalTargets(ctx)
	if !errors.Is(err, errInjected) || got != nil {
		t.Fatalf("List = %v, %v; want nil and the iteration failure", got, err)
	}
}

func TestGraphicalTargetListScanFailureIsReturned(t *testing.T) {
	t.Parallel()
	e, _, ctx := migratedFaultEngine(t)
	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	// INTEGER affinity keeps a non-numeric string as TEXT, which cannot scan
	// into the record's int64 width.
	if _, err := e.conn.conn.ExecContext(ctx,
		"INSERT INTO cp_graphical_targets(target_id, tenant_id, display_name, protocol, width, height, "+
			"is_system, is_static, config, created_at) VALUES('gt-1','acme','c','rfb','wide',480,0,0,'{}',1.0)"); err != nil {
		t.Fatal(err)
	}
	got, err := e.GraphicalTargetStore(tx).ListGraphicalTargets(ctx)
	if err == nil || got != nil {
		t.Fatalf("List = %v, %v; want nil and a scan error", got, err)
	}
}

func TestGraphicalTargetDeleteRowsAffectedFailureIsReturned(t *testing.T) {
	t.Parallel()
	e, f, ctx := migratedFaultEngine(t)
	f.set(func(f *faults) { f.rowsAffectedErr = "DELETE FROM cp_graphical_targets" })
	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	removed, err := e.GraphicalTargetStore(tx).DeleteGraphicalTarget(ctx, "gt-1")
	if !errors.Is(err, errInjected) || removed {
		t.Fatalf("Delete = %v, %v; want false and the RowsAffected failure", removed, err)
	}
}

func TestConsumeResumeTokenUpdateFailuresAreReturned(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name string
		arm  func(*faults)
	}{
		{"update", func(f *faults) { f.execErr = "UPDATE cp_resume_tokens SET revoked_at" }},
		{"rows affected", func(f *faults) { f.rowsAffectedErr = "UPDATE cp_resume_tokens SET revoked_at" }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			e, f, ctx := migratedFaultEngine(t)
			tx, err := e.Begin(ctx)
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = tx.Rollback(ctx) }()
			store := e.TokenStore(tx)
			if err := store.CreateResumeToken(ctx, resumeRecord("r1")); err != nil {
				t.Fatal(err)
			}
			f.set(tc.arm)
			got, err := store.ConsumeResumeToken(ctx, "r1", 5)
			if !errors.Is(err, errInjected) || got != nil {
				t.Fatalf("Consume = %v, %v; want nil and the injected failure", got, err)
			}
		})
	}
}

func TestResolveDatabasePathKeepsUndecodablePath(t *testing.T) {
	t.Parallel()
	// url.Parse decodes %25 to a literal '%', leaving "/%zz" — which is not a
	// valid escape, so the second unescape fails and the decoded path is used.
	if got := ResolveDatabasePath("sqlite:///%25zz"); got != "/%zz" {
		t.Fatalf("ResolveDatabasePath = %q, want /%%zz", got)
	}
}
