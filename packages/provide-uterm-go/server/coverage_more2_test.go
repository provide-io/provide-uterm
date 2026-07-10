//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"errors"
	"net/http"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// registerHijackWorker registers a hijack-mode worker + REST lease on an
// already-defined session without re-adding the session definition (so its
// visibility/owner are preserved). Returns the hijack id.
func registerHijackWorker(t *testing.T, ts *testServer, id string) string {
	t.Helper()
	ctx := context.Background()
	ws := &fakeWorkerWS{}
	if _, err := ts.hub.RegisterWorker(ctx, id, ws); err != nil {
		t.Fatalf("register worker: %v", err)
	}
	if _, _, err := ts.hub.SetInputMode(ctx, id, "hijack"); err != nil {
		t.Fatalf("set input mode: %v", err)
	}
	rec := ts.do("POST", "/worker/"+id+"/hijack/acquire", `{}`, adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("acquire: %d %s", rec.Code, rec.Body.String())
	}
	hid, _ := decode(t, rec.Body.Bytes())["hijack_id"].(string)
	return hid
}

// TestBridgeInvalidWorkerID covers the bridgeParams worker_id 422 branch across
// every bridge REST route.
func TestBridgeInvalidWorkerID(t *testing.T) {
	ts := newTestServer(t, nil)
	hid := "00000000-0000-4000-8000-000000000000"
	cases := []struct{ method, path string }{
		{"POST", "/worker/BAD!ID/hijack/acquire"},
		{"POST", "/worker/BAD!ID/hijack/" + hid + "/heartbeat"},
		{"GET", "/worker/BAD!ID/hijack/" + hid + "/snapshot"},
		{"GET", "/worker/BAD!ID/hijack/" + hid + "/events"},
		{"POST", "/worker/BAD!ID/hijack/" + hid + "/send"},
		{"POST", "/worker/BAD!ID/hijack/" + hid + "/step"},
		{"POST", "/worker/BAD!ID/hijack/" + hid + "/release"},
	}
	for _, c := range cases {
		if rec := ts.do(c.method, c.path, `{}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
			t.Fatalf("%s %s: %d", c.method, c.path, rec.Code)
		}
	}
}

// TestHijackAcquireRateLimited covers the acquire rate-limit branch.
func TestHijackAcquireRateLimited(t *testing.T) {
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Hub = hub.NewTermHub(hub.TermHubConfig{
			Clock:                      deps.Clock,
			OnMetric:                   deps.Metrics.Inc,
			Logger:                     deps.Logger,
			RestAcquireRateLimitPerSec: 1,
		})
	})
	ts.setupWorker(t, "rl")
	// First acquire consumes the single token.
	if rec := ts.do("POST", "/worker/rl/hijack/acquire", `{}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("first acquire: %d %s", rec.Code, rec.Body.String())
	}
	// Second acquire → rate limited.
	if rec := ts.do("POST", "/worker/rl/hijack/acquire", `{}`, adminHeaders()); rec.Code != http.StatusTooManyRequests {
		t.Fatalf("acquire rate limit: %d", rec.Code)
	}
}

// TestHubRouteReadAndModeDenials covers authorizeHubRoute's hubRead + hubMode
// forbidden branches.
func TestHubRouteReadAndModeDenials(t *testing.T) {
	ts := newTestServer(t, nil)
	// Private session with a live REST lease acquired by an admin.
	ts.reg.add("priv", "admin1", "private")
	hid := registerHijackWorker(t, ts, "priv")
	// Viewer read (snapshot/events) → hubRead denial 403.
	if rec := ts.do("GET", "/worker/priv/hijack/"+hid+"/snapshot", "", viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("hubRead denial: %d", rec.Code)
	}
	// Viewer input_mode → hubMode denial 403.
	if rec := ts.do("POST", "/worker/priv/input_mode", `{"input_mode":"open"}`, viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("hubMode denial: %d", rec.Code)
	}
}

// TestReleaseBySessionOwner covers mayReleaseLease's session-owner branch: the
// lease is acquired by an admin but released by the (non-acquirer) session
// owner.
func TestReleaseBySessionOwner(t *testing.T) {
	ts := newTestServer(t, nil)
	// Session owned by "bob"; the worker is registered in hijack mode and the
	// lease is acquired by an admin (AcquiredBy = admin1).
	ts.reg.add("s", "bob", "public")
	hid := registerHijackWorker(t, ts, "s")
	// The session owner "bob" releases. bob is an admin (so authorizeHubRoute
	// passes), but mayReleaseLease returns via the def.Owner == requester branch
	// before the IsAdmin fallback, since bob is the owner but not the acquirer.
	owner := map[string]string{"X-Subject": "bob", "X-Role": "admin"}
	if rec := ts.do("POST", "/worker/s/hijack/"+hid+"/release", "", owner); rec.Code != http.StatusOK {
		t.Fatalf("owner release: %d %s", rec.Code, rec.Body.String())
	}
}

// TestProfileNoStoreAndBadID covers profilesEnabled 503 + requireID 422 across
// the profile routes.
func TestProfileNoStoreAndBadID(t *testing.T) {
	// No store → 503 on every route.
	nostore := newTestServer(t, nil)
	for _, c := range []struct{ method, path, body string }{
		{"GET", "/api/profiles/pf", ""},
		{"POST", "/api/profiles", `{"name":"x"}`},
		{"PUT", "/api/profiles/pf", `{}`},
		{"DELETE", "/api/profiles/pf", ""},
		{"POST", "/api/profiles/pf/connect", `{}`},
	} {
		if rec := nostore.do(c.method, c.path, c.body, adminHeaders()); rec.Code != http.StatusServiceUnavailable {
			t.Fatalf("no store %s %s: %d", c.method, c.path, rec.Code)
		}
	}
	// With a store, an invalid profile id → 422 (requireID).
	store := &fakeProfileStore{}
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.Profiles = store })
	for _, c := range []struct{ method, path, body string }{
		{"GET", "/api/profiles/bad!id", ""},
		{"PUT", "/api/profiles/bad!id", `{}`},
		{"DELETE", "/api/profiles/bad!id", ""},
		{"POST", "/api/profiles/bad!id/connect", `{}`},
	} {
		if rec := ts.do(c.method, c.path, c.body, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
			t.Fatalf("bad id %s %s: %d", c.method, c.path, rec.Code)
		}
	}
}

// TestProfileConnectForbiddenAndUsername covers the connect authz denial + the
// username connector-config branch.
func TestProfileConnectForbiddenAndUsername(t *testing.T) {
	host := "h.example"
	port := 2222
	user := "deploy"
	store := &fakeProfileStore{profile: &serverconfig.ConnectionProfile{
		ProfileID: "pf", Owner: "other", Visibility: "shared", ConnectorType: "ssh",
		Name: "P", Host: &host, Port: &port, Username: &user,
	}}
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.Profiles = store })
	// Viewer can read the shared profile but cannot create a session → 403.
	if rec := ts.do("POST", "/api/profiles/pf/connect", `{}`, viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("connect forbidden: %d", rec.Code)
	}
	// Admin connect → session created with the profile host/port/username.
	if rec := ts.do("POST", "/api/profiles/pf/connect", `{"password":"pw"}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("connect: %d %s", rec.Code, rec.Body.String())
	}
	if len(ts.reg.created) == 0 {
		t.Fatal("no session created")
	}
	cc, _ := ts.reg.created[0]["connector_config"].(map[string]any)
	if cc["username"] != "deploy" || cc["host"] != "h.example" {
		t.Fatalf("connector_config: %v", cc)
	}
}

// TestWebhookErrorBranches covers the no-manager register gate, ValidatePattern
// error, Register error, and unregister invalid-id branches.
func TestWebhookErrorBranches(t *testing.T) {
	// No manager → register 503 (webhookGate manager-nil).
	ns := newTestServer(t, nil)
	ns.reg.add("s1", "admin1", "public")
	if rec := ns.do("POST", "/api/sessions/s1/webhooks", `{"url":"http://x"}`, adminHeaders()); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("register no manager: %d", rec.Code)
	}

	wh := &fakeWebhooks{getOK: false}
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.Webhooks = wh })
	ts.reg.add("s1", "admin1", "public")
	// ValidatePattern error → 422.
	wh.patErr = errors.New("bad pattern")
	if rec := ts.do("POST", "/api/sessions/s1/webhooks", `{"url":"http://x","pattern":"("}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("pattern err: %d", rec.Code)
	}
	wh.patErr = nil
	// Register error → 422.
	wh.regErr = errors.New("dup webhook")
	if rec := ts.do("POST", "/api/sessions/s1/webhooks", `{"url":"http://x"}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("register err: %d", rec.Code)
	}
	wh.regErr = nil
	// Unregister with an invalid webhook id → 422 (requireID).
	if rec := ts.do("DELETE", "/api/sessions/s1/webhooks/bad!id", "", adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("unregister bad id: %d", rec.Code)
	}
}

// TestQuickConnectRecordingAndError covers the recording_enabled branch + the
// writeCreateError path of quick connect.
func TestQuickConnectRecordingAndError(t *testing.T) {
	ts := newTestServer(t, nil)
	// recording_enabled true is threaded into the create payload.
	if rec := ts.do("POST", "/api/connect", `{"connector_type":"telnet","host":"h","recording_enabled":true}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("quick connect recording: %d %s", rec.Code, rec.Body.String())
	}
	if len(ts.reg.created) == 0 || ts.reg.created[0]["recording_enabled"] != true {
		t.Fatalf("recording_enabled not threaded: %v", ts.reg.created)
	}
	// Registry create error → mapped by writeCreateError.
	ts.reg.createErr = &SessionValidationError{Msg: "bad connector"}
	if rec := ts.do("POST", "/api/connect", `{"connector_type":"telnet"}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("quick connect err: %d", rec.Code)
	}
	ts.reg.createErr = nil
}
