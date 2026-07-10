//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// TestSessionControlDenialsAndErrors covers the shared 403/404 branches of the
// per-session control + read handlers.
func TestSessionControlDenialsAndErrors(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("pub", "admin1", "public")
	ts.reg.add("priv", "admin1", "private")

	// Viewer cannot mutate → 403 (gatedSession denial).
	for _, path := range []string{"/connect", "/disconnect", "/restart", "/clear", "/mode", "/annotate"} {
		if rec := ts.do("POST", "/api/sessions/pub"+path, `{"input_mode":"open","label":"x"}`, viewerHeaders()); rec.Code != http.StatusForbidden {
			t.Fatalf("viewer %s: %d", path, rec.Code)
		}
	}
	// Control on unknown session → 404 (definitionOr404).
	if rec := ts.do("POST", "/api/sessions/ghost/connect", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("connect unknown: %d", rec.Code)
	}
	// Registry error on a control op → 404 (statusOrNotFound error path).
	ts.reg.controlErr = ErrSessionNotFound
	if rec := ts.do("POST", "/api/sessions/pub/connect", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("connect control err: %d", rec.Code)
	}
	ts.reg.controlErr = nil

	// Read routes: viewer on private → 403 (readableSession denial).
	for _, path := range []string{"/snapshot", "/events", "/events/watch"} {
		if rec := ts.do("GET", "/api/sessions/priv"+path, "", viewerHeaders()); rec.Code != http.StatusForbidden {
			t.Fatalf("viewer read %s: %d", path, rec.Code)
		}
	}
	// Read routes on unknown → 404.
	for _, path := range []string{"/snapshot", "/events", "/events/watch"} {
		if rec := ts.do("GET", "/api/sessions/ghost"+path, "", adminHeaders()); rec.Code != http.StatusNotFound {
			t.Fatalf("read unknown %s: %d", path, rec.Code)
		}
	}
	// Snapshot registry error → 404.
	ts.reg.snapErr = ErrSessionNotFound
	if rec := ts.do("GET", "/api/sessions/pub/snapshot", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("snapshot err: %d", rec.Code)
	}
	ts.reg.snapErr = nil

	// Patch/get/delete denial + unknown.
	if rec := ts.do("PATCH", "/api/sessions/pub", `{}`, viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer patch: %d", rec.Code)
	}
	if rec := ts.do("DELETE", "/api/sessions/pub", "", viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer delete: %d", rec.Code)
	}
	if rec := ts.do("GET", "/api/sessions/ghost", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("get unknown: %d", rec.Code)
	}
}

// TestProfileDenials covers the profile 403/404 branches.
func TestProfileDenials(t *testing.T) {
	dir := t.TempDir()
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Profiles = serverconfig.NewFileProfileStore(dir)
	})
	// Viewer cannot create.
	if rec := ts.do("POST", "/api/profiles", `{"name":"p"}`, viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer create profile: %d", rec.Code)
	}
	// Unknown get/update/delete/connect → 404.
	for _, m := range []struct{ method, path, body string }{
		{"GET", "/api/profiles/ghost", ""},
		{"PUT", "/api/profiles/ghost", `{}`},
		{"DELETE", "/api/profiles/ghost", ""},
		{"POST", "/api/profiles/ghost/connect", `{}`},
	} {
		if rec := ts.do(m.method, m.path, m.body, adminHeaders()); rec.Code != http.StatusNotFound {
			t.Fatalf("%s %s: %d", m.method, m.path, rec.Code)
		}
	}
}

// TestBridgeParamValidation covers the hijack-id pattern 422 + unknown-session
// authz 404 branches of the bridge routes.
func TestBridgeParamValidation(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("w1", "admin1", "public")
	// Invalid hijack id → 422.
	if rec := ts.do("POST", "/worker/w1/hijack/BADID!/heartbeat", `{}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("bad hijack id: %d", rec.Code)
	}
	// Read route on session with no worker/lease → 404 (no rest session).
	valid := "00000000-0000-4000-8000-000000000000"
	if rec := ts.do("GET", "/worker/w1/hijack/"+valid+"/snapshot", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("snapshot no session: %d", rec.Code)
	}
	if rec := ts.do("GET", "/worker/w1/hijack/"+valid+"/events", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("events no session: %d", rec.Code)
	}
	if rec := ts.do("POST", "/worker/w1/hijack/"+valid+"/send", `{"keys":"x"}`, adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("send no session: %d", rec.Code)
	}
	if rec := ts.do("POST", "/worker/w1/hijack/"+valid+"/step", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("step no session: %d", rec.Code)
	}
	// Unknown session-def → authz 404 on a read route.
	if rec := ts.do("GET", "/worker/nodef/hijack/"+valid+"/snapshot", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("snapshot nodef: %d", rec.Code)
	}
}
