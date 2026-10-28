//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"encoding/json"
	"net/http"
	"reflect"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// decode unmarshals a JSON response body into a map.
func decode(t *testing.T, body []byte) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(body, &m); err != nil {
		t.Fatalf("decode %q: %v", string(body), err)
	}
	return m
}

func TestHealthLifecycle(t *testing.T) {
	ts := newTestServer(t, nil)

	// Not ready yet.
	rec := ts.do("GET", "/api/health", "", nil)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("health before ready: got %d", rec.Code)
	}
	if got := decode(t, rec.Body.Bytes())["status"]; got != "starting" {
		t.Fatalf("status = %v", got)
	}
	if rec := ts.do("GET", "/readyz", "", nil); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("readyz before ready: %d", rec.Code)
	}

	ts.srv.MarkReady()
	rec = ts.do("GET", "/api/health", "", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("health ready: %d", rec.Code)
	}
	body := decode(t, rec.Body.Bytes())
	uptime, active := body["uptime_s"], body["active_sessions"]
	want := map[string]any{
		"status": "ok", "ok": true, "ready": true, "service": "uterm-server", "version": "9.9.9",
		"uptime_s": uptime, "active_sessions": active, "control_plane_backend": "memory",
		"features": []any{"viewers.revoke.subject"},
	}
	if !reflect.DeepEqual(body, want) {
		t.Fatalf("health body: %v", body)
	}
	if rec := ts.do("GET", "/readyz", "", nil); rec.Code != http.StatusOK {
		t.Fatalf("readyz ready: %d", rec.Code)
	}
	if rec := ts.do("GET", "/healthz", "", nil); rec.Code != http.StatusOK {
		t.Fatalf("healthz: %d", rec.Code)
	}
}

func TestHealthAdvertisesFeatures(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()

	rec := ts.do("GET", "/api/health", "", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("health ready: %d", rec.Code)
	}
	body := decode(t, rec.Body.Bytes())
	features, ok := body["features"]
	if !ok {
		t.Fatalf("health missing features key: %v", body)
	}
	arr, ok := features.([]any)
	if !ok {
		t.Fatalf("features not an array: %v", features)
	}
	// sessions.previous is always advertised: ended capture sessions are kept
	// for the owner fallback and the portal's "previous sessions" view.
	if len(arr) == 0 {
		t.Fatalf("features empty, expected at least sessions.previous: %v", arr)
	}
	hasPrevious := false
	for _, f := range arr {
		if f == "sessions.previous" {
			hasPrevious = true
		}
	}
	if !hasPrevious {
		t.Fatalf("features missing sessions.previous: %v", arr)
	}
}
func TestSecurityPosture(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()

	// Anonymous → 401.
	if rec := ts.do("GET", "/api/security-posture", "", nil); rec.Code != http.StatusUnauthorized {
		t.Fatalf("anon posture: %d", rec.Code)
	}
	// Admin → full posture (mode present).
	rec := ts.do("GET", "/api/security-posture", "", adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("admin posture: %d", rec.Code)
	}
	if _, ok := decode(t, rec.Body.Bytes())["mode"]; !ok {
		t.Fatalf("admin posture missing mode: %s", rec.Body.String())
	}
	// Viewer → coarse (no mode).
	rec = ts.do("GET", "/api/security-posture", "", viewerHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("viewer posture: %d", rec.Code)
	}
	body := decode(t, rec.Body.Bytes())
	if _, ok := body["mode"]; ok {
		t.Fatalf("viewer posture leaked mode: %v", body)
	}
	if body["environment"] == nil || body["secure"] == nil {
		t.Fatalf("viewer posture missing coarse fields: %v", body)
	}
}

// "capture.socket_mode" is listed when the capture sockets let the socket
// directory's group connect, and not for owner-only ones, so a producer running
// as another user can tell whether it will be able to connect.
func TestHealthListsTheCaptureSocketModeOnlyWhenItIsOwnerAndGroup(t *testing.T) {
	for _, tc := range []struct {
		mode int
		want []any
	}{
		{0o600, []any{"viewers.revoke.subject"}},
		{0o660, []any{"viewers.revoke.subject", "capture.socket_mode"}},
	} {
		ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
			cfg.Pam.CaptureSocketMode = tc.mode
		})
		ts.srv.MarkReady()
		if got := decode(t, ts.do("GET", "/api/health", "", nil).Body.Bytes())["features"]; !reflect.DeepEqual(got, tc.want) {
			t.Errorf("mode %o: features %v", tc.mode, got)
		}
	}
}
