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

func wrapProbe(mw *AuthMiddleware) (http.Handler, *bool) {
	called := false
	inner := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		called = true
		w.WriteHeader(http.StatusOK)
	})
	return mw.Wrap(inner), &called
}

func newMW() *AuthMiddleware {
	return &AuthMiddleware{
		token:          "secret",
		publicPaths:    map[string]struct{}{"/dashboard": {}},
		publicPrefixes: []string{"/static/"},
	}
}

func TestAuthPublicAndOptions(t *testing.T) {
	h, called := wrapProbe(newMW())
	for _, tc := range []struct{ method, path string }{
		{"GET", "/dashboard"},
		{"GET", "/static/app.js"},
		{"OPTIONS", "/agents"},
	} {
		*called = false
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(tc.method, tc.path, nil))
		if !*called {
			t.Fatalf("%s %s should pass through", tc.method, tc.path)
		}
	}
}

func TestAuthBearerAndXApiToken(t *testing.T) {
	h, called := wrapProbe(newMW())
	for _, hdr := range []map[string]string{
		{"Authorization": "Bearer secret"},
		{"X-Api-Token": "secret"},
	} {
		*called = false
		req := httptest.NewRequest("GET", "/agents", nil)
		for k, v := range hdr {
			req.Header.Set(k, v)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if !*called {
			t.Fatalf("expected auth pass for %v", hdr)
		}
	}
}

func TestAuthRejected(t *testing.T) {
	h, called := wrapProbe(newMW())
	req := httptest.NewRequest("GET", "/agents", nil)
	req.Header.Set("Authorization", "Bearer wrong")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if *called || rec.Code != 401 {
		t.Fatalf("expected 401, got %d called=%v", rec.Code, *called)
	}
	// No header at all.
	*called = false
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/agents", nil))
	if *called || rec.Code != 401 {
		t.Fatalf("no-header: %d", rec.Code)
	}
}

func TestAuthWorkerSelfReportTokens(t *testing.T) {
	secret := "fleet" // pragma: allowlist secret
	wt := secret
	mw := &AuthMiddleware{token: "op", workerToken: &wt, workerSecret: &secret}
	h, called := wrapProbe(mw)
	// Per-agent derived token accepted on self-report path.
	derived := deriveAgentToken(secret, "agent_001")
	req := httptest.NewRequest("POST", "/agent/agent_001/status", nil)
	req.Header.Set("Authorization", "Bearer "+derived)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if !*called {
		t.Fatal("derived token should be accepted")
	}
	// Raw fleet token accepted when enforcement off.
	*called = false
	req = httptest.NewRequest("POST", "/agent/agent_001/register", nil)
	req.Header.Set("Authorization", "Bearer "+secret)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if !*called {
		t.Fatal("raw fleet token should be accepted (enforcement off)")
	}
	// Worker token rejected on an operator route.
	*called = false
	req = httptest.NewRequest("GET", "/agents", nil)
	req.Header.Set("Authorization", "Bearer "+secret)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if *called || rec.Code != 401 {
		t.Fatalf("worker token must be rejected on operator route: %d", rec.Code)
	}
}

func TestAuthEnforcePerAgentRejectsRaw(t *testing.T) {
	secret := "fleet" // pragma: allowlist secret
	wt := secret
	mw := &AuthMiddleware{token: "op", workerToken: &wt, workerSecret: &secret, enforcePerAgent: true}
	h, called := wrapProbe(mw)
	req := httptest.NewRequest("POST", "/agent/agent_001/status", nil)
	req.Header.Set("Authorization", "Bearer "+secret)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if *called || rec.Code != 401 {
		t.Fatalf("raw fleet token should be rejected under enforcement: %d", rec.Code)
	}
}

func TestSetupAuthLoopbackAndTokens(t *testing.T) {
	cfg := DefaultManagerConfig()
	// No token, loopback → skip (nil, nil).
	mw, err := SetupAuth(&cfg, cfg.AuthTokenEnvVar, func(string) string { return "" })
	if err != nil || mw != nil {
		t.Fatalf("loopback skip: mw=%v err=%v", mw, err)
	}
	// No token, non-loopback → error.
	cfg.Host = "0.0.0.0"
	if _, err := SetupAuth(&cfg, cfg.AuthTokenEnvVar, func(string) string { return "" }); err == nil {
		t.Fatal("non-loopback without token must error")
	}
	// Opt-out allows unauthenticated.
	mw, err = SetupAuth(&cfg, cfg.AuthTokenEnvVar, func(k string) string {
		if k == allowUnauthEnvVar {
			return "1"
		}
		return ""
	})
	if err != nil || mw != nil {
		t.Fatalf("opt-out: mw=%v err=%v", mw, err)
	}
	// Token present → middleware installed.
	mw, err = SetupAuth(&cfg, cfg.AuthTokenEnvVar, func(k string) string {
		if k == cfg.AuthTokenEnvVar {
			return "tok"
		}
		return ""
	})
	if err != nil || mw == nil {
		t.Fatalf("token present: mw=%v err=%v", mw, err)
	}
}

func TestIsLoopbackBind(t *testing.T) {
	for _, h := range []string{"127.0.0.1", "localhost", "::1", " LOCALHOST "} {
		if !isLoopbackBind(h) {
			t.Fatalf("%q should be loopback", h)
		}
	}
	for _, h := range []string{"", "0.0.0.0", "10.0.0.5"} {
		if isLoopbackBind(h) {
			t.Fatalf("%q should not be loopback", h)
		}
	}
}
