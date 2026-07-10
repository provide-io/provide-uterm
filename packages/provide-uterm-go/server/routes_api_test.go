//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func TestMetricsOpenAndGated(t *testing.T) {
	ts := newTestServer(t, nil)
	// Default: metrics not auth-gated → anonymous 200.
	rec := ts.do("GET", "/api/metrics", "", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("open metrics: %d", rec.Code)
	}
	if _, ok := decode(t, rec.Body.Bytes())["metrics"]; !ok {
		t.Fatalf("metrics body: %s", rec.Body.String())
	}
	// Prometheus text form contains a seeded counter line.
	rec = ts.do("GET", "/api/metrics/prometheus", "", nil)
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "text/plain; version=0.0.4") {
		t.Fatalf("prom content-type: %q", ct)
	}
	if !strings.Contains(rec.Body.String(), "# TYPE http_requests_total counter") {
		t.Fatalf("prom body: %s", rec.Body.String())
	}

	// Gated: anonymous 401, admin 200.
	gated := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.Security.MetricsRequireAuth = true
	})
	if rec := gated.do("GET", "/api/metrics", "", nil); rec.Code != http.StatusUnauthorized {
		t.Fatalf("gated anon metrics: %d", rec.Code)
	}
	if rec := gated.do("GET", "/api/metrics/prometheus", "", nil); rec.Code != http.StatusUnauthorized {
		t.Fatalf("gated anon prom: %d", rec.Code)
	}
	if rec := gated.do("GET", "/api/metrics", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("gated admin metrics: %d", rec.Code)
	}
}

func TestRequestIDAndSecurityHeaders(t *testing.T) {
	ts := newTestServer(t, nil)
	// Generated request id echoed in the response.
	rec := ts.do("GET", "/healthz", "", nil)
	if rec.Header().Get("X-Request-ID") == "" {
		t.Fatalf("missing generated X-Request-ID")
	}
	if rec.Header().Get("X-Frame-Options") != "DENY" || rec.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Fatalf("missing strict security headers: %v", rec.Header())
	}
	// Inbound request id preserved.
	rec = ts.do("GET", "/healthz", "", map[string]string{"X-Request-ID": "abc123"})
	if got := rec.Header().Get("X-Request-ID"); got != "abc123" {
		t.Fatalf("request id not preserved: %q", got)
	}
	// http_requests_total incremented.
	if ts.metrics.Snapshot()["http_requests_total"] < 2 {
		t.Fatalf("http_requests_total not counted")
	}
}

func TestCORSAndOriginGate(t *testing.T) {
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.Server.AllowedOrigins = []string{"https://app.example"}
	})
	// Preflight from an allowed origin.
	rec := ts.do("OPTIONS", "/api/sessions", "", map[string]string{"Origin": "https://app.example"})
	if rec.Code != http.StatusOK {
		t.Fatalf("preflight: %d", rec.Code)
	}
	if rec.Header().Get("Access-Control-Allow-Origin") != "https://app.example" {
		t.Fatalf("CORS origin header: %v", rec.Header())
	}

	// WS upgrade from a disallowed cross-origin → 403.
	rec = ts.do("GET", "/ws/browser/w1/term", "", map[string]string{
		"Upgrade": "websocket", "Origin": "https://evil.example",
	})
	if rec.Code != http.StatusForbidden {
		t.Fatalf("origin gate: %d", rec.Code)
	}
}

func TestUnauthenticatedRejected(t *testing.T) {
	ts := newTestServer(t, nil)
	for _, path := range []string{"/api/sessions", "/api/profiles", "/api/keys"} {
		if rec := ts.do("GET", path, "", nil); rec.Code != http.StatusUnauthorized {
			t.Fatalf("%s anon: got %d want 401", path, rec.Code)
		}
	}
	if ts.metrics.Snapshot()["auth_failures_http_total"] < 3 {
		t.Fatalf("auth failures not counted")
	}
}

func TestNewValidatesDeps(t *testing.T) {
	if _, err := New(Deps{}); err == nil {
		t.Fatal("expected error for missing Hub")
	}
}
