//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func TestApprovals(t *testing.T) {
	ts := newTestServer(t, nil)
	future := ts.srv.clock.Wall() + 3600
	ts.hub.Approvals.Add(&hub.ApprovalRequest{ID: "r1", WorkerID: "w1", SubmitterID: "someoneelse", Command: "ls", Status: hub.ApprovalPending, ExpiresAt: future})
	ts.hub.Approvals.Add(&hub.ApprovalRequest{ID: "own", WorkerID: "w1", SubmitterID: "admin1", Command: "rm", Status: hub.ApprovalPending, ExpiresAt: future})
	ts.hub.Approvals.Add(&hub.ApprovalRequest{ID: "r2", WorkerID: "w1", SubmitterID: "x", Command: "y", Status: hub.ApprovalPending, ExpiresAt: future})

	// Viewer forbidden.
	if rec := ts.do("POST", "/api/approvals/r1/approve", "", viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer approve: %d", rec.Code)
	}
	// Self-approval blocked.
	if rec := ts.do("POST", "/api/approvals/own/approve", "", adminHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("self approve: %d", rec.Code)
	}
	// Approve someone else's → 200.
	if rec := ts.do("POST", "/api/approvals/r1/approve", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("approve: %d %s", rec.Code, rec.Body.String())
	}
	// Second approve → 400 not pending.
	if rec := ts.do("POST", "/api/approvals/r1/approve", "", adminHeaders()); rec.Code != http.StatusBadRequest {
		t.Fatalf("re-approve: %d", rec.Code)
	}
	// Approve unknown → 404.
	if rec := ts.do("POST", "/api/approvals/ghost/approve", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("approve ghost: %d", rec.Code)
	}
	// Reject flow.
	if rec := ts.do("POST", "/api/approvals/r2/reject", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("reject: %d", rec.Code)
	}
	if rec := ts.do("POST", "/api/approvals/r2/reject", "", adminHeaders()); rec.Code != http.StatusBadRequest {
		t.Fatalf("re-reject: %d", rec.Code)
	}
	if rec := ts.do("POST", "/api/approvals/ghost/reject", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("reject ghost: %d", rec.Code)
	}
}

func TestAPIKeys(t *testing.T) {
	ts := newTestServer(t, nil)
	// Viewer forbidden.
	if rec := ts.do("POST", "/api/keys", `{"name":"k","scopes":["viewer"]}`, viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer create key: %d", rec.Code)
	}
	// Create ok.
	rec := ts.do("POST", "/api/keys", `{"name":"k","scopes":["viewer","operator"]}`, adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("create key: %d %s", rec.Code, rec.Body.String())
	}
	body := decode(t, rec.Body.Bytes())
	if body["key"] == nil || body["key_id"] == nil {
		t.Fatalf("key body: %v", body)
	}
	keyID, _ := body["key_id"].(string)
	// List.
	if rec := ts.do("GET", "/api/keys", "", adminHeaders()); rec.Code != http.StatusOK || len(decodeArray(t, rec.Body.Bytes())) != 1 {
		t.Fatalf("list keys: %d %s", rec.Code, rec.Body.String())
	}
	// Revoke ok + unknown.
	if rec := ts.do("DELETE", "/api/keys/"+keyID, "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("revoke: %d", rec.Code)
	}
	if rec := ts.do("DELETE", "/api/keys/ghost", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("revoke ghost: %d", rec.Code)
	}

	// Validation branches.
	bad := []struct {
		body string
	}{
		{`{"scopes":["viewer"]}`},                             // no name
		{`{"name":"k"}`},                                      // no scopes
		{`{"name":"k","scopes":"x"}`},                         // scopes not list
		{`{"name":"k","scopes":[]}`},                          // empty scopes
		{`{"name":"k","scopes":["nope"]}`},                    // invalid scope
		{`{"name":"k","scopes":["viewer"],"expires_in_s":5}`}, // too short
	}
	for _, c := range bad {
		if rec := ts.do("POST", "/api/keys", c.body, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
			t.Fatalf("expected 422 for %s got %d", c.body, rec.Code)
		}
	}

	// Disabled → 403.
	disabled := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.Auth.APIKeysEnabled = false // pragma: allowlist secret
	})
	if rec := disabled.do("GET", "/api/keys", "", adminHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("disabled keys: %d", rec.Code)
	}
}

func TestProfiles(t *testing.T) {
	dir := t.TempDir()
	store := serverconfig.NewFileProfileStore(dir)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Profiles = store
	})

	// Create (admin).
	rec := ts.do("POST", "/api/profiles", `{"name":"p1","connector_type":"ssh","host":"h","port":22}`, adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("create profile: %d %s", rec.Code, rec.Body.String())
	}
	id, _ := decode(t, rec.Body.Bytes())["profile_id"].(string)
	if id == "" {
		t.Fatalf("no profile id")
	}
	// List.
	if rec := ts.do("GET", "/api/profiles", "", adminHeaders()); rec.Code != http.StatusOK || len(decodeArray(t, rec.Body.Bytes())) != 1 {
		t.Fatalf("list profiles: %d %s", rec.Code, rec.Body.String())
	}
	// Get ok + unknown.
	if rec := ts.do("GET", "/api/profiles/"+id, "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("get profile: %d", rec.Code)
	}
	if rec := ts.do("GET", "/api/profiles/ghost", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("get ghost: %d", rec.Code)
	}
	// Update.
	if rec := ts.do("PUT", "/api/profiles/"+id, `{"name":"renamed"}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("update: %d %s", rec.Code, rec.Body.String())
	}
	// Connect (creates a session).
	if rec := ts.do("POST", "/api/profiles/"+id+"/connect", `{"password":"pw"}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("connect: %d %s", rec.Code, rec.Body.String())
	}
	// Delete.
	if rec := ts.do("DELETE", "/api/profiles/"+id, "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("delete: %d", rec.Code)
	}
	// No store → 503.
	nostore := newTestServer(t, nil)
	if rec := nostore.do("GET", "/api/profiles", "", adminHeaders()); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("no store: %d", rec.Code)
	}
}

func TestQuickConnect(t *testing.T) {
	ts := newTestServer(t, nil)
	// Viewer forbidden.
	if rec := ts.do("POST", "/api/connect", `{"connector_type":"ssh"}`, viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer connect: %d", rec.Code)
	}
	// Admin ok.
	rec := ts.do("POST", "/api/connect", `{"connector_type":"telnet","host":"h","display_name":"d"}`, adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("quick connect: %d %s", rec.Code, rec.Body.String())
	}
	body := decode(t, rec.Body.Bytes())
	if body["session_id"] == nil || body["url"] == nil {
		t.Fatalf("connect body: %v", body)
	}
}
