//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnel"
)

// farFuture is an expires_at well past any wall clock, so a token stays valid
// for the duration of a test (real tokens carry created_at + ttl).
const farFuture = 1e18

// shareReq builds a GET request for path with an optional uterm_tunnel_{id}
// cookie. httptest sets RemoteAddr to 192.0.2.1:1234, so sourceIP → 192.0.2.1.
func shareReq(path, cookieName, cookieVal string) *http.Request {
	r := httptest.NewRequest(http.MethodGet, path, http.NoBody)
	if cookieName != "" {
		r.AddCookie(&http.Cookie{Name: cookieName, Value: cookieVal})
	}
	return r
}

func TestShareSessionIDFor(t *testing.T) {
	cases := map[string]string{
		"/api/sessions/s1":          "s1",
		"/api/sessions/s1/status":   "s1",
		"/app/session/s2":           "s2",
		"/app/operator/s2":          "s2",
		"/app/replay/s2":            "s2",
		"/app/inspect/s2":           "s2",
		"/ws/browser/s3/term":       "s3",
		"/worker/s4/hijack":         "s4",
		"/worker/s4/hijack/acquire": "s4",
		"/app/connect":              "",
		"/app/":                     "",
		"/healthz":                  "",
	}
	for path, want := range cases {
		if got := shareSessionIDFor(path); got != want {
			t.Errorf("shareSessionIDFor(%q)=%q want %q", path, got, want)
		}
	}
}

func TestResolveSharePrincipal(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.deps.TunnelStore.PutToken("s1", tunnel.TokenRecord{
		ControlTokenHash: tunnel.HashToken("ctrl-tok"),
		ShareTokenHash:   tunnel.HashToken("view-tok"),
		ExpiresAt:        farFuture,
	})

	// Control token → operator, admin confined to this session.
	op := ts.srv.resolveShareprincipal(shareReq("/app/session/s1", "uterm_tunnel_s1", "ctrl-tok"))
	if op == nil || op.SubjectID != "share:s1:operator" {
		t.Fatalf("operator principal = %+v", op)
	}
	if !op.Roles.Has("admin") || !op.Scopes.Has("*") {
		t.Fatalf("operator roles/scopes = %v / %v", op.Roles.Sorted(), op.Scopes.Sorted())
	}
	if op.AdminSessionScope == nil || *op.AdminSessionScope != "s1" {
		t.Fatalf("operator AdminSessionScope = %v, want s1", op.AdminSessionScope)
	}

	// Share token → viewer, read-only, NOT a scoped admin. Exercises a second
	// path pattern (/api/sessions/{id}).
	vw := ts.srv.resolveShareprincipal(shareReq("/api/sessions/s1", "uterm_tunnel_s1", "view-tok"))
	if vw == nil || vw.SubjectID != "share:s1:viewer" {
		t.Fatalf("viewer principal = %+v", vw)
	}
	if !vw.Roles.Has("viewer") || !vw.Scopes.Has("session.read") || vw.AdminSessionScope != nil {
		t.Fatalf("viewer principal shape wrong: %+v", vw)
	}

	// Wrong token, absent cookie, empty cookie, non-share path, and unknown
	// session all fall through (nil → configured IdP runs).
	nilCases := []struct {
		name             string
		path, ck, ckWant string
	}{
		{"wrong-token", "/app/session/s1", "uterm_tunnel_s1", "nope"},
		{"no-cookie", "/app/session/s1", "", ""},
		{"empty-cookie", "/app/session/s1", "uterm_tunnel_s1", ""},
		{"non-share-path", "/healthz", "uterm_tunnel_s1", "ctrl-tok"},
		{"unknown-session", "/app/session/nope", "uterm_tunnel_nope", "ctrl-tok"},
	}
	for _, c := range nilCases {
		if p := ts.srv.resolveShareprincipal(shareReq(c.path, c.ck, c.ckWant)); p != nil {
			t.Errorf("%s: expected nil, got %+v", c.name, p)
		}
	}
}

func TestResolveSharePrincipalExpired(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.deps.TunnelStore.PutToken("s1", tunnel.TokenRecord{
		ControlTokenHash: tunnel.HashToken("ctrl-tok"),
		ExpiresAt:        1.0, // 1970 — long expired
	})
	if p := ts.srv.resolveShareprincipal(shareReq("/app/session/s1", "uterm_tunnel_s1", "ctrl-tok")); p != nil {
		t.Fatalf("expired token must not authenticate, got %+v", p)
	}
}

func TestResolveSharePrincipalIPBinding(t *testing.T) {
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) { cfg.Tunnel.IPBinding = true })
	other := "9.9.9.9"
	same := "192.0.2.1" // httptest RemoteAddr host
	empty := ""
	ts.srv.deps.TunnelStore.PutToken("mismatch", tunnel.TokenRecord{ControlTokenHash: tunnel.HashToken("c"), IssuedIP: &other, ExpiresAt: farFuture})
	ts.srv.deps.TunnelStore.PutToken("match", tunnel.TokenRecord{ControlTokenHash: tunnel.HashToken("c"), IssuedIP: &same, ExpiresAt: farFuture})
	ts.srv.deps.TunnelStore.PutToken("nobind", tunnel.TokenRecord{ControlTokenHash: tunnel.HashToken("c"), IssuedIP: &empty, ExpiresAt: farFuture})

	if p := ts.srv.resolveShareprincipal(shareReq("/app/session/mismatch", "uterm_tunnel_mismatch", "c")); p != nil {
		t.Fatalf("IP mismatch must reject, got %+v", p)
	}
	if p := ts.srv.resolveShareprincipal(shareReq("/app/session/match", "uterm_tunnel_match", "c")); p == nil {
		t.Fatal("matching IP must authenticate")
	}
	// An empty issued IP means the token was not bound — it must not reject even
	// with IP binding enabled.
	if p := ts.srv.resolveShareprincipal(shareReq("/app/session/nobind", "uterm_tunnel_nobind", "c")); p == nil {
		t.Fatal("empty issued IP must not reject")
	}
}

func TestShareRoleOf(t *testing.T) {
	cases := []struct {
		subject string
		want    string
	}{
		{"share:s1:operator", "operator"},
		{"share:s1:viewer", "viewer"},
		{"admin1", ""},
		{"share:", ""}, // trailing colon, no role
	}
	for _, c := range cases {
		if got := shareRoleOf(&serverauth.Principal{SubjectID: c.subject}); got != c.want {
			t.Errorf("shareRoleOf(%q)=%q want %q", c.subject, got, c.want)
		}
	}
	if got := shareRoleOf(nil); got != "" {
		t.Errorf("shareRoleOf(nil)=%q want empty", got)
	}

	// sharePageRole: non-empty role → the string; otherwise nil (JSON null).
	if v := sharePageRole(&serverauth.Principal{SubjectID: "share:s1:viewer"}); v != "viewer" {
		t.Errorf("sharePageRole(viewer)=%v want viewer", v)
	}
	if v := sharePageRole(&serverauth.Principal{SubjectID: "admin1"}); v != nil {
		t.Errorf("sharePageRole(non-share)=%v want nil", v)
	}
}

// TestSharePageEmitsShareRole is the end-to-end proof: a request carrying only
// the tunnel-share cookie (no IdP header) authenticates via the share principal
// and the page bootstrap carries its role.
func TestSharePageEmitsShareRole(t *testing.T) {
	dir := t.TempDir()
	writeViteManifest(t, dir)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.FrontendDir = dir })
	ts.reg.add("s1", "admin1", "public")
	ts.srv.deps.TunnelStore.PutToken("s1", tunnel.TokenRecord{
		ControlTokenHash: tunnel.HashToken("ctrl-tok"),
		ShareTokenHash:   tunnel.HashToken("view-tok"),
		ExpiresAt:        farFuture,
	})

	// Operator cookie → operator page renders with share_role "operator", with
	// NO IdP header present (proves the share principal alone authorizes).
	rec := ts.do("GET", "/app/operator/s1", "", map[string]string{"Cookie": "uterm_tunnel_s1=ctrl-tok"})
	if rec.Code != http.StatusOK {
		t.Fatalf("operator page status %d", rec.Code)
	}
	if role := extractBootstrap(t, rec.Body.String())["share_role"]; role != "operator" {
		t.Fatalf("operator share_role=%v want operator", role)
	}

	// Viewer cookie on the user session page → share_role "viewer".
	rec = ts.do("GET", "/app/session/s1", "", map[string]string{"Cookie": "uterm_tunnel_s1=view-tok"})
	if rec.Code != http.StatusOK {
		t.Fatalf("session page status %d", rec.Code)
	}
	if role := extractBootstrap(t, rec.Body.String())["share_role"]; role != "viewer" {
		t.Fatalf("viewer share_role=%v want viewer", role)
	}
}
