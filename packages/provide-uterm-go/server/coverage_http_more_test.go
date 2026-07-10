//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"errors"
	"net/http"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// fakeProfileStore is a ProfileStore with per-method error/return injection for
// the profile handler error branches.
type fakeProfileStore struct {
	listErr, getErr, createErr, updateErr, deleteErr error
	profile                                          *serverconfig.ConnectionProfile
	updated                                          *serverconfig.ConnectionProfile
	updateNil                                        bool
}

func (f *fakeProfileStore) ListProfiles(*string) ([]serverconfig.ConnectionProfile, error) {
	if f.listErr != nil {
		return nil, f.listErr
	}
	return []serverconfig.ConnectionProfile{}, nil
}

func (f *fakeProfileStore) GetProfile(string) (*serverconfig.ConnectionProfile, error) {
	return f.profile, f.getErr
}

func (f *fakeProfileStore) CreateProfile(p serverconfig.ConnectionProfile) (*serverconfig.ConnectionProfile, error) {
	if f.createErr != nil {
		return nil, f.createErr
	}
	return &p, nil
}

func (f *fakeProfileStore) UpdateProfile(string, map[string]any) (*serverconfig.ConnectionProfile, error) {
	if f.updateErr != nil {
		return nil, f.updateErr
	}
	if f.updateNil {
		return nil, nil
	}
	return f.updated, nil
}

func (f *fakeProfileStore) DeleteProfile(string) (bool, error) { return true, f.deleteErr }

// TestProfileErrorBranches drives the profile-handler error/authz branches with
// an injectable store.
func TestProfileErrorBranches(t *testing.T) {
	store := &fakeProfileStore{}
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.Profiles = store })

	// Non-admin (viewer) list → owner-scoped path.
	if rec := ts.do("GET", "/api/profiles", "", viewerHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("viewer list: %d", rec.Code)
	}
	// List backend error → 500.
	store.listErr = errors.New("db down")
	if rec := ts.do("GET", "/api/profiles", "", adminHeaders()); rec.Code != http.StatusInternalServerError {
		t.Fatalf("list err: %d", rec.Code)
	}
	store.listErr = nil

	// Create without a port → optPort nil branch; CreateProfile error → 422.
	store.createErr = errors.New("invalid profile")
	if rec := ts.do("POST", "/api/profiles", `{"name":"p","connector_type":"local"}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("create err: %d %s", rec.Code, rec.Body.String())
	}
	store.createErr = nil

	// Get: a private profile owned by someone else, read by a viewer → 403.
	other := "someoneelse"
	store.profile = &serverconfig.ConnectionProfile{ProfileID: "pf", Owner: other, Visibility: "private"}
	if rec := ts.do("GET", "/api/profiles/pf", "", viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("get forbidden: %d", rec.Code)
	}
	// Update: viewer cannot mutate another's profile → 403.
	if rec := ts.do("PUT", "/api/profiles/pf", `{"name":"x"}`, viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("update forbidden: %d", rec.Code)
	}
	// Delete: viewer cannot mutate → 403.
	if rec := ts.do("DELETE", "/api/profiles/pf", "", viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("delete forbidden: %d", rec.Code)
	}

	// Admin owns the store profile now (for the update/connect success/err paths).
	store.profile = &serverconfig.ConnectionProfile{ProfileID: "pf", Owner: "admin1", Visibility: "private", ConnectorType: "ssh", Name: "P"}
	// Update backend error → 422.
	store.updateErr = errors.New("bad update")
	if rec := ts.do("PUT", "/api/profiles/pf", `{"name":"x"}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("update err: %d", rec.Code)
	}
	store.updateErr = nil
	// Update returns nil (vanished mid-flight) → 404.
	store.updateNil = true
	if rec := ts.do("PUT", "/api/profiles/pf", `{"name":"x"}`, adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("update nil: %d", rec.Code)
	}
	store.updateNil = false

	// Connect: CreateSession registry error → mapped by writeCreateError.
	ts.reg.createErr = &SessionConflictError{Msg: "dup"}
	if rec := ts.do("POST", "/api/profiles/pf/connect", `{"password":"pw"}`, adminHeaders()); rec.Code != http.StatusConflict {
		t.Fatalf("connect conflict: %d %s", rec.Code, rec.Body.String())
	}
	ts.reg.createErr = nil
}

// TestCreateSessionErrorMapping covers writeCreateError's egress + default
// branches and the invalid-body branch.
func TestCreateSessionErrorMapping(t *testing.T) {
	ts := newTestServer(t, nil)
	// Malformed body → 422.
	if rec := ts.do("POST", "/api/sessions", "{bad", adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("bad body: %d", rec.Code)
	}
	// Egress-blocked → 422.
	ts.reg.createErr = &EgressBlockedError{Msg: "blocked host"}
	if rec := ts.do("POST", "/api/sessions", `{"connector_type":"ssh"}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("egress: %d", rec.Code)
	}
	// Generic error → 500 (default arm).
	ts.reg.createErr = errors.New("boom")
	if rec := ts.do("POST", "/api/sessions", `{"connector_type":"ssh"}`, adminHeaders()); rec.Code != http.StatusInternalServerError {
		t.Fatalf("generic: %d", rec.Code)
	}
	ts.reg.createErr = nil
}

// TestPatchSessionErrorBranches covers the patch validation/not-found + body
// branches.
func TestPatchSessionErrorBranches(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("s1", "admin1", "public")
	// Malformed body → 422.
	if rec := ts.do("PATCH", "/api/sessions/s1", "{bad", adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("patch bad body: %d", rec.Code)
	}
	// Validation error → 422.
	ts.reg.updateErr = &SessionValidationError{Msg: "nope"}
	if rec := ts.do("PATCH", "/api/sessions/s1", `{"display_name":"x"}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("patch validation: %d", rec.Code)
	}
	// Generic error → 404.
	ts.reg.updateErr = errors.New("gone")
	if rec := ts.do("PATCH", "/api/sessions/s1", `{"display_name":"x"}`, adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("patch generic: %d", rec.Code)
	}
	ts.reg.updateErr = nil
}

// TestSessionListFilters covers the visibility/state filters + session_id sort.
func TestSessionListFilters(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("alpha", "admin1", "public")
	ts.reg.add("beta", "admin1", "private")
	ts.reg.statuses["beta"].LifecycleState = "stopped"

	// visibility filter drops the private one.
	if rec := ts.do("GET", "/api/sessions?visibility=public", "", adminHeaders()); rec.Code != http.StatusOK || len(decodeArray(t, rec.Body.Bytes())) != 1 {
		t.Fatalf("visibility filter: %s", rec.Body.String())
	}
	// state filter drops the running one.
	if rec := ts.do("GET", "/api/sessions?state=stopped", "", adminHeaders()); rec.Code != http.StatusOK || len(decodeArray(t, rec.Body.Bytes())) != 1 {
		t.Fatalf("state filter: %s", rec.Body.String())
	}
	// sort by session_id.
	if rec := ts.do("GET", "/api/sessions?sort=session_id&order=asc", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("sort session_id: %d", rec.Code)
	}
}

// TestBulkDeleteExtraBranches covers the malformed-body + older_than filter.
func TestBulkDeleteExtraBranches(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("s1", "admin1", "public")
	old := ts.srv.clock.Wall() - 100
	ts.reg.statuses["s1"].LifecycleState = "stopped"
	ts.reg.statuses["s1"].StoppedAt = &old

	// Malformed body → 422.
	if rec := ts.do("DELETE", "/api/sessions", "{bad", adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("bulk bad body: %d", rec.Code)
	}
	// older_than filter matches the stopped session.
	rec := ts.do("DELETE", "/api/sessions", `{"filter":{"older_than_s":10}}`, adminHeaders())
	if rec.Code != http.StatusOK || decode(t, rec.Body.Bytes())["deleted"] != float64(1) {
		t.Fatalf("older_than: %d %s", rec.Code, rec.Body.String())
	}
}

// TestSessionControlBodyAndRegistryErrors covers the invalid-body branches of
// mode/annotate and the registry-error branches of analyze/events/watch.
func TestSessionControlBodyAndRegistryErrors(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("s1", "admin1", "public")

	// Mode: malformed body → 422; invalid mode value → 422.
	if rec := ts.do("POST", "/api/sessions/s1/mode", "{bad", adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("mode bad body: %d", rec.Code)
	}
	if rec := ts.do("POST", "/api/sessions/s1/mode", `{"input_mode":"weird"}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("mode bad value: %d", rec.Code)
	}
	// Annotate: malformed body → 422.
	if rec := ts.do("POST", "/api/sessions/s1/annotate", "{bad", adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("annotate bad body: %d", rec.Code)
	}

	// Analyze registry error (def present) → 404.
	ts.reg.analysisErr = errors.New("no runtime")
	if rec := ts.do("POST", "/api/sessions/s1/analyze", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("analyze err: %d", rec.Code)
	}
	ts.reg.analysisErr = nil
	// Events registry error → 404.
	ts.reg.eventsErr = errors.New("no runtime")
	if rec := ts.do("GET", "/api/sessions/s1/events", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("events err: %d", rec.Code)
	}
	ts.reg.eventsErr = nil
	// Watch registry error + event_types query param → 404.
	ts.reg.watchErr = errors.New("no runtime")
	if rec := ts.do("GET", "/api/sessions/s1/events/watch?event_types=a,b&pattern=x", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("watch err: %d", rec.Code)
	}
	ts.reg.watchErr = nil
	// Watch success with event_types param.
	if rec := ts.do("GET", "/api/sessions/s1/events/watch?event_types=a,b", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("watch ok: %d", rec.Code)
	}
}

// TestAPIKeyExtraBranches covers the create body/scope/expiry branches and the
// viewer revoke gate.
func TestAPIKeyExtraBranches(t *testing.T) {
	ts := newTestServer(t, nil)
	// Malformed body → 422.
	if rec := ts.do("POST", "/api/keys", "{bad", adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("key bad body: %d", rec.Code)
	}
	// Non-string scope item is skipped (toString → ""); a valid scope remains.
	if rec := ts.do("POST", "/api/keys", `{"name":"k","scopes":["viewer",123]}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("key skip nonstring scope: %d %s", rec.Code, rec.Body.String())
	}
	// Valid expiry (>=60) → created.
	if rec := ts.do("POST", "/api/keys", `{"name":"k","scopes":["viewer"],"expires_in_s":3600}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("key expiry: %d %s", rec.Code, rec.Body.String())
	}
	// Viewer cannot revoke → 403.
	if rec := ts.do("DELETE", "/api/keys/anything", "", viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer revoke: %d", rec.Code)
	}
}

// TestApprovalRejectViewerGate covers the reject admin gate.
func TestApprovalRejectViewerGate(t *testing.T) {
	ts := newTestServer(t, nil)
	if rec := ts.do("POST", "/api/approvals/r1/reject", "", viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer reject: %d", rec.Code)
	}
}

// TestHealthEmptyBackend covers the backend=="" default in handleHealth.
func TestHealthEmptyBackend(t *testing.T) {
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.ControlPlane.Backend = ""
	})
	ts.srv.MarkReady()
	rec := ts.do("GET", "/api/health", "", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("health: %d", rec.Code)
	}
	if decode(t, rec.Body.Bytes())["control_plane_backend"] != "memory" {
		t.Fatalf("backend default: %s", rec.Body.String())
	}
}

// TestNewValidatesEachDep covers the remaining New() required-dep branches.
func TestNewValidatesEachDep(t *testing.T) {
	base := func() Deps {
		ts := newTestServer(t, nil)
		return ts.srv.deps
	}
	cases := []func(d *Deps){
		func(d *Deps) { d.Auth = nil },
		func(d *Deps) { d.Authz = nil },
		func(d *Deps) { d.Config = nil },
		func(d *Deps) { d.Registry = nil },
	}
	for i, mut := range cases {
		d := base()
		mut(&d)
		if _, err := New(d); err == nil {
			t.Fatalf("case %d: expected error", i)
		}
	}
}
