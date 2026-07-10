//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"net/http"
	"sync"
	"testing"
)

// fakeWorkerWS records control frames the hub sends to the worker.
type fakeWorkerWS struct {
	mu   sync.Mutex
	sent []string
}

func (f *fakeWorkerWS) SendText(_ context.Context, payload string) error {
	f.mu.Lock()
	f.sent = append(f.sent, payload)
	f.mu.Unlock()
	return nil
}

// setupWorker registers a worker + its session definition and puts it in hijack
// input mode so REST acquire succeeds.
func (ts *testServer) setupWorker(t *testing.T, id string) *fakeWorkerWS {
	t.Helper()
	ts.reg.add(id, "admin1", "public")
	ws := &fakeWorkerWS{}
	if _, err := ts.hub.RegisterWorker(context.Background(), id, ws); err != nil {
		t.Fatalf("register worker: %v", err)
	}
	if _, _, err := ts.hub.SetInputMode(context.Background(), id, "hijack"); err != nil {
		t.Fatalf("set input mode: %v", err)
	}
	return ws
}

func TestBridgeHijackLifecycle(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.setupWorker(t, "w1")

	// Viewer cannot acquire.
	if rec := ts.do("POST", "/worker/w1/hijack/acquire", `{}`, viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer acquire: %d", rec.Code)
	}
	// Admin acquires.
	rec := ts.do("POST", "/worker/w1/hijack/acquire", `{"owner":"tester","lease_s":120}`, adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("acquire: %d %s", rec.Code, rec.Body.String())
	}
	body := decode(t, rec.Body.Bytes())
	hid, _ := body["hijack_id"].(string)
	if hid == "" || body["ok"] != true {
		t.Fatalf("acquire body: %v", body)
	}
	// Second acquire → 409 already hijacked.
	if rec := ts.do("POST", "/worker/w1/hijack/acquire", `{}`, adminHeaders()); rec.Code != http.StatusConflict {
		t.Fatalf("double acquire: %d", rec.Code)
	}
	// Heartbeat ok + unknown hijack id.
	if rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/heartbeat", `{"lease_s":90}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("heartbeat: %d %s", rec.Code, rec.Body.String())
	}
	ghost := "00000000-0000-4000-8000-000000000000"
	if rec := ts.do("POST", "/worker/w1/hijack/"+ghost+"/heartbeat", `{}`, adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("heartbeat ghost: %d", rec.Code)
	}
	// Snapshot + events (read cap).
	if rec := ts.do("GET", "/worker/w1/hijack/"+hid+"/snapshot?wait_ms=50", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("snapshot: %d %s", rec.Code, rec.Body.String())
	}
	if rec := ts.do("GET", "/worker/w1/hijack/"+hid+"/events", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("events: %d %s", rec.Code, rec.Body.String())
	}
	// Send: empty keys → 400; real keys → 200.
	if rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/send", `{"keys":""}`, adminHeaders()); rec.Code != http.StatusBadRequest {
		t.Fatalf("send empty: %d", rec.Code)
	}
	if rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/send", `{"keys":"ls\n","timeout_ms":100,"poll_interval_ms":50}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("send: %d %s", rec.Code, rec.Body.String())
	}
	// Step.
	if rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/step", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("step: %d %s", rec.Code, rec.Body.String())
	}
	// Release (owner) → 200, then again → 404.
	if rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/release", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("release: %d %s", rec.Code, rec.Body.String())
	}
	if rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/release", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("double release: %d", rec.Code)
	}
}

func TestBridgeAcquireNoWorker(t *testing.T) {
	ts := newTestServer(t, nil)
	// Session definition exists (so authz passes) but no worker registered.
	ts.reg.add("w9", "admin1", "public")
	rec := ts.do("POST", "/worker/w9/hijack/acquire", `{}`, adminHeaders())
	if rec.Code != http.StatusConflict {
		t.Fatalf("acquire no worker: %d %s", rec.Code, rec.Body.String())
	}
	if decode(t, rec.Body.Bytes())["error"] == nil {
		t.Fatalf("expected error body: %s", rec.Body.String())
	}
}

func TestBridgeReleaseNotOwner(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.setupWorker(t, "w1")
	rec := ts.do("POST", "/worker/w1/hijack/acquire", `{}`, adminHeaders())
	hid, _ := decode(t, rec.Body.Bytes())["hijack_id"].(string)
	// A different admin is allowed to release (admin override); a lease-owner
	// denial requires a non-admin that still passes authz, which the default
	// RBAC does not grant on a session they don't own — so we assert the admin
	// override path succeeds here.
	if rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/release", "", map[string]string{"X-Subject": "admin2", "X-Role": "admin"}); rec.Code != http.StatusOK {
		t.Fatalf("admin override release: %d %s", rec.Code, rec.Body.String())
	}
}

func TestBridgeWorkerControl(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.setupWorker(t, "w1")

	// input_mode ok + invalid value.
	if rec := ts.do("POST", "/worker/w1/input_mode", `{"input_mode":"open"}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("input_mode: %d %s", rec.Code, rec.Body.String())
	}
	if rec := ts.do("POST", "/worker/w1/input_mode", `{"input_mode":"bogus"}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("input_mode invalid: %d", rec.Code)
	}
	// input_mode on a session with no registered worker → 404.
	ts.reg.add("w2", "admin1", "public")
	if rec := ts.do("POST", "/worker/w2/input_mode", `{"input_mode":"open"}`, adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("input_mode no worker: %d %s", rec.Code, rec.Body.String())
	}

	// disconnect_worker: viewer 403, admin 200, unknown 404.
	if rec := ts.do("POST", "/worker/w1/disconnect_worker", "", viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("disconnect viewer: %d", rec.Code)
	}
	if rec := ts.do("POST", "/worker/w1/disconnect_worker", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("disconnect: %d %s", rec.Code, rec.Body.String())
	}
	if rec := ts.do("POST", "/worker/w2/disconnect_worker", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("disconnect unknown: %d", rec.Code)
	}

	// Unknown session (no definition) → authz 404.
	if rec := ts.do("POST", "/worker/nodef/hijack/acquire", `{}`, adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("acquire nodef: %d", rec.Code)
	}
}
