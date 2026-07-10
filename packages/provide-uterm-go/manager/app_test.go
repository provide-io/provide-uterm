//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestCreateManagerAppOK(t *testing.T) {
	cfg := DefaultManagerConfig()
	cfg.TimeseriesDir = t.TempDir()
	_, handler, err := CreateManagerApp(cfg, AppOptions{Getenv: func(string) string { return "" }})
	if err != nil {
		t.Fatalf("create app: %v", err)
	}
	// Loopback + no token → auth skipped, /health reachable.
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest("GET", "/health", nil))
	if rec.Code != 200 {
		t.Fatalf("health via app: %d", rec.Code)
	}
}

func TestCreateManagerAppAuthEnforced(t *testing.T) {
	cfg := DefaultManagerConfig()
	cfg.TimeseriesDir = t.TempDir()
	getenv := func(k string) string {
		if k == cfg.AuthTokenEnvVar {
			return "tok"
		}
		return ""
	}
	_, handler, err := CreateManagerApp(cfg, AppOptions{Getenv: getenv})
	if err != nil {
		t.Fatalf("create app: %v", err)
	}
	// No token → 401.
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest("GET", "/agents", nil))
	if rec.Code != 401 {
		t.Fatalf("expected 401, got %d", rec.Code)
	}
	// With token → 200.
	rec = httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/agents", nil)
	req.Header.Set("Authorization", "Bearer tok")
	handler.ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("expected 200 with token, got %d", rec.Code)
	}
}

func TestCreateManagerAppCORSErrors(t *testing.T) {
	cfg := DefaultManagerConfig()
	cfg.TimeseriesDir = t.TempDir()
	cfg.CORSOrigins = nil
	if _, _, err := CreateManagerApp(cfg, AppOptions{Getenv: func(string) string { return "" }}); err == nil {
		t.Fatal("empty CORS should error")
	}
	cfg.CORSOrigins = []string{"*"}
	if _, _, err := CreateManagerApp(cfg, AppOptions{Getenv: func(string) string { return "" }}); err == nil {
		t.Fatal("wildcard CORS should error")
	}
}

func TestCreateManagerAppNonLoopbackNoTokenErrors(t *testing.T) {
	cfg := DefaultManagerConfig()
	cfg.TimeseriesDir = t.TempDir()
	cfg.Host = "0.0.0.0"
	if _, _, err := CreateManagerApp(cfg, AppOptions{Getenv: func(string) string { return "" }}); err == nil {
		t.Fatal("non-loopback without token should error")
	}
}

func TestCORSPreflightAndOrigin(t *testing.T) {
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(204) })
	h := corsMiddleware([]string{"http://ok.example"}, next)

	// Preflight from allowed origin.
	req := httptest.NewRequest("OPTIONS", "/agents", nil)
	req.Header.Set("Origin", "http://ok.example")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != 200 || rec.Header().Get("Access-Control-Allow-Origin") != "http://ok.example" {
		t.Fatalf("preflight: %d %q", rec.Code, rec.Header().Get("Access-Control-Allow-Origin"))
	}
	// Non-preflight passes through.
	req = httptest.NewRequest("GET", "/agents", nil)
	req.Header.Set("Origin", "http://ok.example")
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != 204 {
		t.Fatalf("passthrough: %d", rec.Code)
	}
}
