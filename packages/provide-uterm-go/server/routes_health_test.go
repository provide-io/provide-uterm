//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"encoding/json"
	"net/http"
	"testing"
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
	if body["status"] != "ok" || body["version"] != "9.9.9" || body["control_plane_backend"] != "memory" {
		t.Fatalf("health body: %v", body)
	}
	if rec := ts.do("GET", "/readyz", "", nil); rec.Code != http.StatusOK {
		t.Fatalf("readyz ready: %d", rec.Code)
	}
	if rec := ts.do("GET", "/healthz", "", nil); rec.Code != http.StatusOK {
		t.Fatalf("healthz: %d", rec.Code)
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
