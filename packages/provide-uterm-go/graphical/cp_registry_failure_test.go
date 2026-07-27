//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package graphical_test

import (
	"context"
	"errors"
	"testing"
	"time"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/graphical"
)

// The real engines only fail on genuine I/O faults, so the registry's
// backend-error branches are unreachable through them. These fakes inject the
// failures directly — without this, a store error silently returning the wrong
// code would go unnoticed.

var errBackend = errors.New("backend exploded")

// failMode selects which operation the fake store fails.
type failMode int

const (
	failNone failMode = iota
	failGet
	failList
	failPut
	failDelete
	failBegin
	failCommit
)

type fakeStore struct {
	mode failMode
	rows map[string]cp.GraphicalTargetRecord
}

func (s *fakeStore) PutGraphicalTarget(_ context.Context, rec cp.GraphicalTargetRecord) error {
	if s.mode == failPut {
		return errBackend
	}
	s.rows[rec.TargetID] = rec
	return nil
}

func (s *fakeStore) GetGraphicalTarget(_ context.Context, id string) (*cp.GraphicalTargetRecord, error) {
	if s.mode == failGet {
		return nil, errBackend
	}
	rec, ok := s.rows[id]
	if !ok {
		return nil, nil
	}
	return &rec, nil
}

func (s *fakeStore) ListGraphicalTargets(_ context.Context) ([]cp.GraphicalTargetRecord, error) {
	if s.mode == failList {
		return nil, errBackend
	}
	out := make([]cp.GraphicalTargetRecord, 0, len(s.rows))
	for _, rec := range s.rows {
		out = append(out, rec)
	}
	return out, nil
}

func (s *fakeStore) DeleteGraphicalTarget(_ context.Context, id string) (bool, error) {
	if s.mode == failDelete {
		return false, errBackend
	}
	_, ok := s.rows[id]
	delete(s.rows, id)
	return ok, nil
}

type fakeTx struct{ failCommit bool }

func (t *fakeTx) Commit(context.Context) error {
	if t.failCommit {
		return errBackend
	}
	return nil
}
func (t *fakeTx) Rollback(context.Context) error { return nil }

type fakeEngine struct {
	mode  failMode
	store *fakeStore
}

func newFakeEngine(mode failMode) *fakeEngine {
	return &fakeEngine{mode: mode, store: &fakeStore{mode: mode, rows: map[string]cp.GraphicalTargetRecord{}}}
}

func (e *fakeEngine) Capabilities() cp.EngineCapabilities { return cp.DefaultCapabilities() }
func (e *fakeEngine) Open(context.Context) error          { return nil }
func (e *fakeEngine) Close(context.Context) error         { return nil }
func (e *fakeEngine) Migrate(context.Context) error       { return nil }

func (e *fakeEngine) Begin(context.Context) (cp.Tx, error) {
	if e.mode == failBegin {
		return nil, errBackend
	}
	return &fakeTx{failCommit: e.mode == failCommit}, nil
}

func (e *fakeEngine) Reap(context.Context, float64, int) (int, error)     { return 0, nil }
func (e *fakeEngine) GetAuditHead(context.Context) (*cp.AuditHead, error) { return nil, nil }
func (e *fakeEngine) SetAuditHead(context.Context, int64, string) error   { return nil }
func (e *fakeEngine) SessionStore(cp.Tx) cp.SessionStore                  { return nil }
func (e *fakeEngine) TokenStore(cp.Tx) cp.TokenStore                      { return nil }
func (e *fakeEngine) ApprovalStore(cp.Tx) cp.ApprovalStore                { return nil }
func (e *fakeEngine) LeaseStore(cp.Tx) cp.LeaseStore                      { return nil }

func (e *fakeEngine) GraphicalTargetStore(cp.Tx) cp.GraphicalTargetStore { return e.store }

func failingRegistry(t *testing.T, mode failMode) (*graphical.ControlPlaneRegistry, graphical.Scope) {
	t.Helper()
	r := graphical.NewControlPlaneRegistry(newFakeEngine(mode))
	scope, ok := graphical.ScopeForTenant("acme")
	if !ok {
		t.Fatal("scope")
	}
	return r, scope
}

func wantCode(t *testing.T, err error, want graphical.ErrorCode, label string) {
	t.Helper()
	var ge *graphical.Error
	if !errors.As(err, &ge) {
		t.Fatalf("%s: err = %v, want *graphical.Error", label, err)
	}
	if ge.Code != want {
		t.Fatalf("%s: code = %v, want %v", label, ge.Code, want)
	}
}

// A store read failure must surface as CodeBackend on every verb that reads,
// and must never be mistaken for "absent".
func TestCPRegistryStoreReadFailureIsBackendError(t *testing.T) {
	t.Parallel()
	r, scope := failingRegistry(t, failGet)
	_, err := r.Get(scope, "gt-1")
	wantCode(t, err, graphical.CodeBackend, "Get")

	_, err = r.Create(scope, def("gt-1", "acme"))
	wantCode(t, err, graphical.CodeBackend, "Create")

	_, err = r.Update(scope, def("gt-1", "acme"))
	wantCode(t, err, graphical.CodeBackend, "Update")

	err = r.Delete(scope, "gt-1")
	wantCode(t, err, graphical.CodeBackend, "Delete")
}

func TestCPRegistryListFailureIsBackendError(t *testing.T) {
	t.Parallel()
	r, scope := failingRegistry(t, failList)
	_, err := r.List(scope)
	wantCode(t, err, graphical.CodeBackend, "List")
}

func TestCPRegistryWriteFailureIsBackendError(t *testing.T) {
	t.Parallel()
	r, scope := failingRegistry(t, failPut)
	_, err := r.Create(scope, def("gt-1", "acme"))
	wantCode(t, err, graphical.CodeBackend, "Create")
}

func TestCPRegistryDeleteFailureIsBackendError(t *testing.T) {
	t.Parallel()
	r, scope := failingRegistry(t, failDelete)
	// Seed through a non-failing path so Delete reaches the store call itself.
	seed := newFakeEngine(failNone)
	seed.store.mode = failNone
	r2 := graphical.NewControlPlaneRegistry(seed)
	if _, err := r2.Create(scope, def("gt-1", "acme")); err != nil {
		t.Fatalf("seed create: %v", err)
	}
	seed.store.mode = failDelete
	wantCode(t, r2.Delete(scope, "gt-1"), graphical.CodeBackend, "Delete")

	_ = r // the failDelete registry above is covered by the seeded variant
}

// Begin failing means no transaction was ever opened.
func TestCPRegistryBeginFailureIsBackendError(t *testing.T) {
	t.Parallel()
	r, scope := failingRegistry(t, failBegin)
	_, err := r.Get(scope, "gt-1")
	wantCode(t, err, graphical.CodeBackend, "Get")

	_, err = r.List(scope)
	wantCode(t, err, graphical.CodeBackend, "List")

	_, err = r.Create(scope, def("gt-1", "acme"))
	wantCode(t, err, graphical.CodeBackend, "Create")

	_, err = r.Update(scope, def("gt-1", "acme"))
	wantCode(t, err, graphical.CodeBackend, "Update")

	wantCode(t, r.Delete(scope, "gt-1"), graphical.CodeBackend, "Delete")
}

// A commit failure is a conflict, not a backend fault: the write was well-formed
// and the caller can retry it.
func TestCPRegistryCommitFailureIsConflict(t *testing.T) {
	t.Parallel()
	r, scope := failingRegistry(t, failCommit)
	_, err := r.Create(scope, def("gt-1", "acme"))
	wantCode(t, err, graphical.CodeConflict, "Create")
}

// A row whose config is not a JSON object must degrade to an empty map rather
// than failing the read — one bad row cannot take out a listing.
func TestCPRegistryNonObjectConfigDegrades(t *testing.T) {
	t.Parallel()
	engine := newFakeEngine(failNone)
	scope, _ := graphical.ScopeForTenant("acme")
	for _, blob := range []string{"[1,2,3]", "not-json", `"str"`, ""} {
		engine.store.rows["gt-1"] = cp.GraphicalTargetRecord{
			TargetID: "gt-1", TenantID: "acme", DisplayName: "c",
			Protocol: graphical.ProtocolMemory, Width: 640, Height: 480,
			Config: blob, CreatedAt: 100,
		}
		got, err := graphical.NewControlPlaneRegistry(engine).Get(scope, "gt-1")
		if err != nil {
			t.Fatalf("config %q: %v", blob, err)
		}
		if got == nil || len(got.Config) != 0 {
			t.Fatalf("config %q should degrade to empty, got %+v", blob, got)
		}
	}
}

// Zero timestamps map to 0 rather than a large negative epoch, and survive the
// round trip as the zero time.
func TestCPRegistryZeroTimestampsRoundTrip(t *testing.T) {
	t.Parallel()
	engine := newFakeEngine(failNone)
	scope, _ := graphical.ScopeForTenant("acme")
	r := graphical.NewControlPlaneRegistry(engine)
	r.SetClock(func() time.Time { return time.Time{} })

	if _, err := r.Create(scope, def("gt-1", "acme")); err != nil {
		t.Fatalf("create: %v", err)
	}
	if stored := engine.store.rows["gt-1"]; stored.CreatedAt != 0 {
		t.Fatalf("zero time stored as %v, want 0", stored.CreatedAt)
	}
	got, err := r.Get(scope, "gt-1")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if !got.CreatedAt.IsZero() {
		t.Fatalf("created_at = %v, want zero time", got.CreatedAt)
	}
	if got.UpdatedAt != nil {
		t.Fatalf("updated_at should stay unset, got %v", got.UpdatedAt)
	}
}

// An unserializable config is rejected as invalid rather than panicking or
// silently writing a broken row.
func TestCPRegistryUnserializableConfigIsInvalid(t *testing.T) {
	t.Parallel()
	engine := newFakeEngine(failNone)
	scope, _ := graphical.ScopeForTenant("acme")
	target := def("gt-1", "acme")
	target.Config = map[string]any{"bad": make(chan int)}

	_, err := graphical.NewControlPlaneRegistry(engine).Create(scope, target)
	wantCode(t, err, graphical.CodeInvalid, "Create")
}

// AddStatic must reject a malformed definition and refuse duplicate ids —
// seeding is a programming-time operation, so both are hard errors.
func TestCPRegistryAddStaticRejectsInvalidAndDuplicate(t *testing.T) {
	t.Parallel()
	r := graphical.NewControlPlaneRegistry(newFakeEngine(failNone))

	bad := def("gt-1", "acme")
	bad.TargetID = "not a valid id!"
	wantCode(t, r.AddStatic(bad), graphical.CodeInvalid, "AddStatic invalid")

	if err := r.AddStatic(def("gt-static", "acme")); err != nil {
		t.Fatalf("first AddStatic: %v", err)
	}
	wantCode(t, r.AddStatic(def("gt-static", "acme")), graphical.CodeConflict, "AddStatic duplicate")
}

// Create and Update validate the payload before touching the store.
func TestCPRegistryRejectsInvalidPayload(t *testing.T) {
	t.Parallel()
	r, scope := failingRegistry(t, failNone)
	bad := def("gt-1", "acme")
	bad.TargetID = "not a valid id!"

	_, err := r.Create(scope, bad)
	wantCode(t, err, graphical.CodeInvalid, "Create")

	_, err = r.Update(scope, bad)
	wantCode(t, err, graphical.CodeInvalid, "Update")
}

// A stored row owned by another tenant must not be updatable or deletable even
// when the id is known — the scope check runs against the STORED tenant, not the
// tenant the caller put in the payload.
func TestCPRegistryRejectsCrossTenantStoredRow(t *testing.T) {
	t.Parallel()
	engine := newFakeEngine(failNone)
	engine.store.rows["gt-1"] = cp.GraphicalTargetRecord{
		TargetID: "gt-1", TenantID: "someone-else", DisplayName: "c",
		Protocol: graphical.ProtocolMemory, Width: 640, Height: 480, CreatedAt: 100,
	}
	r := graphical.NewControlPlaneRegistry(engine)
	scope, _ := graphical.ScopeForTenant("acme")

	_, err := r.Update(scope, def("gt-1", "acme"))
	wantCode(t, err, graphical.CodeForbidden, "Update")
	wantCode(t, r.Delete(scope, "gt-1"), graphical.CodeForbidden, "Delete")
}

// An updated row round-trips its updated_at/updated_by stamps back out.
func TestCPRegistryUpdateStampsRoundTrip(t *testing.T) {
	t.Parallel()
	engine := newFakeEngine(failNone)
	r := graphical.NewControlPlaneRegistry(engine)
	r.SetClock(func() time.Time { return fixedTime })
	scope, _ := graphical.ScopeForTenant("acme")

	if _, err := r.Create(scope, def("gt-1", "acme")); err != nil {
		t.Fatalf("create: %v", err)
	}
	next := def("gt-1", "acme")
	editor := "ops"
	next.UpdatedBy = &editor
	if _, err := r.Update(scope, next); err != nil {
		t.Fatalf("update: %v", err)
	}

	got, err := r.Get(scope, "gt-1")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got.UpdatedAt == nil || !got.UpdatedAt.Equal(fixedTime) {
		t.Fatalf("updated_at = %v, want %v", got.UpdatedAt, fixedTime)
	}
	if got.UpdatedBy == nil || *got.UpdatedBy != "ops" {
		t.Fatalf("updated_by = %v", got.UpdatedBy)
	}
}
