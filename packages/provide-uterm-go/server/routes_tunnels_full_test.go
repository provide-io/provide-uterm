//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"crypto/tls"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnel"
)

// tunnelServer builds a test server with a fixed public base URL and a manual
// clock (wall=1000) so URL + expiry assertions are deterministic.
func tunnelServer(t *testing.T) *testServer {
	t.Helper()
	return newTestServer(t, func(cfg *serverconfig.UtermServerConfig, deps *Deps) {
		cfg.Server.PublicBaseURL = "http://tunnel.example"
		cfg.UI.AppPath = "/app"
		deps.Clock = hub.NewManualClock(1000)
	})
}

// bodyMap decodes an httptest recorder body into a map.
func bodyMap(t *testing.T, raw []byte) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("decode body: %v (%s)", err, raw)
	}
	return m
}

func TestCreateTunnelAuth(t *testing.T) {
	ts := tunnelServer(t)
	// anonymous → 401
	if rec := ts.do("POST", "/api/tunnels", `{}`, nil); rec.Code != http.StatusUnauthorized {
		t.Fatalf("anon = %d, want 401", rec.Code)
	}
	// viewer lacks session.control.create → 403
	if rec := ts.do("POST", "/api/tunnels", `{}`, viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer = %d, want 403", rec.Code)
	}
}

func TestCreateTunnelSuccess(t *testing.T) {
	ts := tunnelServer(t)
	rec := ts.do("POST", "/api/tunnels",
		`{"tunnel_type":"terminal","display_name":"demo","ttl_s":120}`, adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("create = %d body=%s", rec.Code, rec.Body.String())
	}
	m := bodyMap(t, rec.Body.Bytes())
	id, _ := m["tunnel_id"].(string)
	if !strings.HasPrefix(id, "tunnel-") {
		t.Fatalf("tunnel_id = %v", m["tunnel_id"])
	}
	if m["display_name"] != "demo" || m["tunnel_type"] != "terminal" {
		t.Fatalf("meta = %+v", m)
	}
	if m["ws_endpoint"] != "ws://tunnel.example/tunnel/"+id {
		t.Fatalf("ws_endpoint = %v", m["ws_endpoint"])
	}
	if wt, _ := m["worker_token"].(string); wt == "" {
		t.Fatal("missing worker_token")
	}
	if exp, _ := m["expires_at"].(float64); exp != 1120 {
		t.Fatalf("expires_at = %v, want 1120", m["expires_at"])
	}
	shareURL, _ := m["share_url"].(string)
	controlURL, _ := m["control_url"].(string)
	if !strings.HasPrefix(shareURL, "http://tunnel.example/s/"+id+"?invite=") {
		t.Fatalf("share_url = %q", shareURL)
	}
	if !strings.Contains(controlURL, "/s/"+id+"?invite=") {
		t.Fatalf("control_url = %q", controlURL)
	}
	// token record stored, hashed.
	reven, ok := ts.srv.deps.TunnelStore.GetToken(id)
	if !ok || reven.ShareTokenHash == "" || reven.TunnelType != "terminal" {
		t.Fatalf("token record = %+v ok=%v", reven, ok)
	}
}

func TestCreateTunnelTTLClamp(t *testing.T) {
	ts := tunnelServer(t) // token_ttl_s default 3600, so max = 3600*24 = 86400
	// below-floor ttl clamps to 60.
	rec := ts.do("POST", "/api/tunnels", `{"ttl_s":1}`, adminHeaders())
	if exp := bodyMap(t, rec.Body.Bytes())["expires_at"].(float64); exp != 1060 {
		t.Fatalf("low ttl expires_at = %v, want 1060", exp)
	}
	// above-ceiling ttl clamps to 86400.
	rec = ts.do("POST", "/api/tunnels", `{"ttl_s":9999999}`, adminHeaders())
	if exp := bodyMap(t, rec.Body.Bytes())["expires_at"].(float64); exp != 1000+86400 {
		t.Fatalf("high ttl expires_at = %v, want %v", exp, 1000+86400)
	}
}

func TestCreateTunnelRegistryErrors(t *testing.T) {
	ts := tunnelServer(t)
	ts.reg.createErr = &SessionValidationError{Msg: "bad"}
	if rec := ts.do("POST", "/api/tunnels", `{}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("validation err = %d, want 422", rec.Code)
	}
	ts.reg.createErr = &SessionConflictError{Msg: "dup"}
	if rec := ts.do("POST", "/api/tunnels", `{}`, adminHeaders()); rec.Code != http.StatusConflict {
		t.Fatalf("conflict err = %d, want 409", rec.Code)
	}
}

func TestCreateTunnelHTTPTypeSharePage(t *testing.T) {
	ts := tunnelServer(t)
	rec := ts.do("POST", "/api/tunnels", `{"tunnel_type":"http"}`, adminHeaders())
	id := bodyMap(t, rec.Body.Bytes())["tunnel_id"].(string)
	entry, _ := ts.srv.deps.TunnelStore.GetToken(id)
	if entry.SharePage != "inspect" {
		t.Fatalf("http share_page = %q, want inspect", entry.SharePage)
	}
}

func TestRevokeTunnelTokens(t *testing.T) {
	ts := tunnelServer(t)

	// Unknown tunnel (no session def, no tokens) → idempotent 200.
	if rec := ts.do("DELETE", "/api/tunnels/ghost/tokens", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("unknown revoke = %d, want 200", rec.Code)
	}

	// Session owned by someone else, non-admin caller → 403.
	ts.reg.add("t-own", "someoneelse", "private")
	ts.srv.deps.TunnelStore.PutToken("t-own", tunnel.TokenRecord{ShareTokenHash: "h"})
	if rec := ts.do("DELETE", "/api/tunnels/t-own/tokens", "", operatorHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("non-owner revoke = %d, want 403", rec.Code)
	}
	// tokens must survive a denied revoke.
	if _, ok := ts.srv.deps.TunnelStore.GetToken("t-own"); !ok {
		t.Fatal("denied revoke must not drop tokens")
	}

	// Owner revoke → 200 and tokens gone.
	ts.reg.add("t-me", "op1", "private")
	ts.srv.deps.TunnelStore.PutToken("t-me", tunnel.TokenRecord{ShareTokenHash: "h"})
	ts.srv.deps.TunnelStore.IssueInvites("t-me", "s", "c", 1e12, 1000, nil)
	rec := ts.do("DELETE", "/api/tunnels/t-me/tokens", "", operatorHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("owner revoke = %d, want 200", rec.Code)
	}
	m := bodyMap(t, rec.Body.Bytes())
	if m["ok"] != true || m["session_id"] != "t-me" {
		t.Fatalf("revoke body = %+v", m)
	}
	if _, ok := ts.srv.deps.TunnelStore.GetToken("t-me"); ok {
		t.Fatal("tokens should be revoked")
	}

	// Admin can revoke a session it does not own.
	ts.reg.add("t-admin", "someoneelse", "private")
	ts.srv.deps.TunnelStore.PutToken("t-admin", tunnel.TokenRecord{ShareTokenHash: "h"})
	if rec := ts.do("DELETE", "/api/tunnels/t-admin/tokens", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("admin revoke = %d, want 200", rec.Code)
	}
}

func TestRotateTunnelTokens(t *testing.T) {
	ts := tunnelServer(t)

	// Unknown session → 404 (unknown session).
	rec := ts.do("POST", "/api/tunnels/ghost/tokens/rotate", "", adminHeaders())
	if rec.Code != http.StatusNotFound || !strings.Contains(rec.Body.String(), "unknown session") {
		t.Fatalf("unknown rotate = %d body=%s", rec.Code, rec.Body.String())
	}

	// Session exists, non-owner non-admin → 403.
	ts.reg.add("r1", "someoneelse", "private")
	if rec := ts.do("POST", "/api/tunnels/r1/tokens/rotate", "", operatorHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("non-owner rotate = %d, want 403", rec.Code)
	}

	// Session exists + owner but no tokens → 404 (no tunnel tokens).
	ts.reg.add("r2", "op1", "private")
	rec = ts.do("POST", "/api/tunnels/r2/tokens/rotate", "", operatorHeaders())
	if rec.Code != http.StatusNotFound || !strings.Contains(rec.Body.String(), "no tunnel tokens") {
		t.Fatalf("no-tokens rotate = %d body=%s", rec.Code, rec.Body.String())
	}

	// Owner with existing tokens → 200 with fresh URLs, old invites discarded.
	ts.reg.add("r3", "op1", "private")
	ts.srv.deps.TunnelStore.PutToken("r3", tunnel.TokenRecord{
		ShareTokenHash: tunnel.HashToken("old-share"), TunnelType: "terminal",
	})
	oldShare, _ := ts.srv.deps.TunnelStore.IssueInvites("r3", "old-share", "old-ctrl", 1e12, 1000, nil)
	rec = ts.do("POST", "/api/tunnels/r3/tokens/rotate", "", operatorHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("owner rotate = %d body=%s", rec.Code, rec.Body.String())
	}
	m := bodyMap(t, rec.Body.Bytes())
	if m["tunnel_id"] != "r3" || m["expires_at"].(float64) != 1000+3600 {
		t.Fatalf("rotate body = %+v", m)
	}
	// Old invite must be gone (discarded on rotate).
	if inv := ts.srv.deps.TunnelStore.ConsumeInvite(oldShare, "r3", 1001); inv != nil {
		t.Fatal("rotate must discard old invites")
	}
	// New token hash differs from the seeded one.
	entry, _ := ts.srv.deps.TunnelStore.GetToken("r3")
	if entry.ShareTokenHash == tunnel.HashToken("old-share") {
		t.Fatal("rotate must replace the share token")
	}
}

func TestShareConsumerNoInvite(t *testing.T) {
	ts := tunnelServer(t)
	ts.srv.deps.TunnelStore.PutToken("sc1", tunnel.TokenRecord{SharePage: "session"})
	// Anonymous, no invite → 302 to session page, no cookie.
	rec := ts.do("GET", "/s/sc1", "", nil)
	if rec.Code != http.StatusFound {
		t.Fatalf("share no-invite = %d, want 302", rec.Code)
	}
	if loc := rec.Header().Get("Location"); loc != "/app/session/sc1" {
		t.Fatalf("Location = %q", loc)
	}
	if len(rec.Result().Cookies()) != 0 {
		t.Fatal("no-invite must not set a cookie")
	}
}

func TestShareConsumerViewerInvite(t *testing.T) {
	ts := tunnelServer(t)
	ts.srv.deps.TunnelStore.PutToken("sc2", tunnel.TokenRecord{
		ShareTokenHash:   tunnel.HashToken("share-tok"),
		ControlTokenHash: tunnel.HashToken("ctrl-tok"),
		SharePage:        "session",
	})
	share, control := ts.srv.deps.TunnelStore.IssueInvites("sc2", "share-tok", "ctrl-tok", 1e12, 1000, nil)

	// Viewer invite → session page + viewer cookie.
	rec := ts.do("GET", "/s/sc2?invite="+url.QueryEscape(share), "", nil)
	if rec.Code != http.StatusFound || rec.Header().Get("Location") != "/app/session/sc2" {
		t.Fatalf("viewer share = %d loc=%q", rec.Code, rec.Header().Get("Location"))
	}
	ck := cookieByName(rec.Result().Cookies(), "uterm_tunnel_sc2")
	if ck == nil || ck.Value != "share-tok" || !ck.HttpOnly {
		t.Fatalf("viewer cookie = %+v", ck)
	}

	// Operator invite → operator page + operator cookie.
	rec = ts.do("GET", "/s/sc2?invite="+url.QueryEscape(control), "", nil)
	if rec.Code != http.StatusFound || rec.Header().Get("Location") != "/app/operator/sc2" {
		t.Fatalf("operator share = %d loc=%q", rec.Code, rec.Header().Get("Location"))
	}
	if ck := cookieByName(rec.Result().Cookies(), "uterm_tunnel_sc2"); ck == nil || ck.Value != "ctrl-tok" {
		t.Fatalf("operator cookie = %+v", ck)
	}
}

func TestShareConsumerHTTPInspectPage(t *testing.T) {
	ts := tunnelServer(t)
	ts.srv.deps.TunnelStore.PutToken("sc-http", tunnel.TokenRecord{
		ShareTokenHash: tunnel.HashToken("share-tok"), SharePage: "inspect",
	})
	share, _ := ts.srv.deps.TunnelStore.IssueInvites("sc-http", "share-tok", "ctrl", 1e12, 1000, nil)
	rec := ts.do("GET", "/s/sc-http?invite="+url.QueryEscape(share), "", nil)
	if rec.Header().Get("Location") != "/app/inspect/sc-http" {
		t.Fatalf("http viewer page = %q", rec.Header().Get("Location"))
	}
}

func TestShareConsumerInvalidInvite(t *testing.T) {
	ts := tunnelServer(t)
	ts.srv.deps.TunnelStore.PutToken("sc3", tunnel.TokenRecord{ShareTokenHash: tunnel.HashToken("x")})
	rec := ts.do("GET", "/s/sc3?invite=not-a-real-invite", "", nil)
	if rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), "invalid or expired") {
		t.Fatalf("invalid invite = %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestShareConsumerStaleInvite(t *testing.T) {
	ts := tunnelServer(t)
	// Issue an invite for "A", then replace the stored token hash with "B"
	// WITHOUT discarding invites → the consumed invite validates but no longer
	// matches the active token (the stale-invite branch).
	ts.srv.deps.TunnelStore.PutToken("sc4", tunnel.TokenRecord{ShareTokenHash: tunnel.HashToken("A")})
	share, _ := ts.srv.deps.TunnelStore.IssueInvites("sc4", "A", "ctrl", 1e12, 1000, nil)
	ts.srv.deps.TunnelStore.PutToken("sc4", tunnel.TokenRecord{ShareTokenHash: tunnel.HashToken("B")})
	rec := ts.do("GET", "/s/sc4?invite="+url.QueryEscape(share), "", nil)
	if rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), "stale invite") {
		t.Fatalf("stale invite = %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestShareConsumerUnknownTunnelDefaultsSession(t *testing.T) {
	ts := tunnelServer(t)
	// No token record at all, no invite → default share page "session".
	rec := ts.do("GET", "/s/unknown-tunnel", "", nil)
	if rec.Code != http.StatusFound || rec.Header().Get("Location") != "/app/session/unknown-tunnel" {
		t.Fatalf("unknown tunnel = %d loc=%q", rec.Code, rec.Header().Get("Location"))
	}
}

func TestListTunnels(t *testing.T) {
	ts := tunnelServer(t)
	ts.reg.add("lt-a", "op1", "private")
	ts.reg.add("lt-b", "someoneelse", "private")
	ts.srv.deps.TunnelStore.PutToken("lt-a", tunnel.TokenRecord{TunnelType: "terminal"})
	ts.srv.deps.TunnelStore.PutToken("lt-b", tunnel.TokenRecord{TunnelType: "http"})

	// Admin sees all.
	rec := ts.do("GET", "/api/tunnels", "", adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("admin list = %d", rec.Code)
	}
	if n := len(bodyList(t, rec.Body.Bytes())); n != 2 {
		t.Fatalf("admin sees %d, want 2", n)
	}

	// Owner op1 sees only lt-a.
	rec = ts.do("GET", "/api/tunnels", "", operatorHeaders())
	list := bodyList(t, rec.Body.Bytes())
	if len(list) != 1 || list[0]["tunnel_id"] != "lt-a" {
		t.Fatalf("owner list = %+v", list)
	}

	// Unrelated viewer sees none.
	rec = ts.do("GET", "/api/tunnels", "", viewerHeaders())
	if n := len(bodyList(t, rec.Body.Bytes())); n != 0 {
		t.Fatalf("viewer sees %d, want 0", n)
	}
}

func TestCreateTunnelIPBinding(t *testing.T) {
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, deps *Deps) {
		cfg.Server.PublicBaseURL = "http://tunnel.example"
		cfg.Tunnel.IPBinding = true
		deps.Clock = hub.NewManualClock(1000)
	})
	rec := ts.do("POST", "/api/tunnels", `{}`, adminHeaders())
	id := bodyMap(t, rec.Body.Bytes())["tunnel_id"].(string)
	entry, _ := ts.srv.deps.TunnelStore.GetToken(id)
	if entry.IssuedIP == nil {
		t.Fatal("ip binding should record an issued IP")
	}
}

func TestRotateTunnelIPBinding(t *testing.T) {
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, deps *Deps) {
		cfg.Server.PublicBaseURL = "http://tunnel.example"
		cfg.Tunnel.IPBinding = true
		deps.Clock = hub.NewManualClock(1000)
	})
	ts.reg.add("rip", "op1", "private")
	ts.srv.deps.TunnelStore.PutToken("rip", tunnel.TokenRecord{ShareTokenHash: tunnel.HashToken("x")})
	rec := ts.do("POST", "/api/tunnels/rip/tokens/rotate", "", operatorHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("rotate = %d", rec.Code)
	}
	entry, _ := ts.srv.deps.TunnelStore.GetToken("rip")
	if entry.IssuedIP == nil {
		t.Fatal("rotate under ip binding should record an issued IP")
	}
}

func TestCreateTunnelBaseURLFallback(t *testing.T) {
	// Empty PublicBaseURL → base derived from the request host.
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, deps *Deps) {
		cfg.Server.PublicBaseURL = ""
		deps.Clock = hub.NewManualClock(1000)
	})
	rec := ts.do("POST", "/api/tunnels", `{}`, adminHeaders())
	m := bodyMap(t, rec.Body.Bytes())
	// httptest.NewRequest defaults the host to example.com.
	if su, _ := m["share_url"].(string); !strings.HasPrefix(su, "http://example.com/s/") {
		t.Fatalf("fallback share_url = %q", su)
	}
	if we, _ := m["ws_endpoint"].(string); !strings.HasPrefix(we, "ws://example.com/tunnel/") {
		t.Fatalf("fallback ws_endpoint = %q", we)
	}
}

func TestSameSiteMode(t *testing.T) {
	cases := map[string]http.SameSite{
		"strict": http.SameSiteStrictMode,
		"STRICT": http.SameSiteStrictMode,
		"none":   http.SameSiteNoneMode,
		"lax":    http.SameSiteLaxMode,
		"":       http.SameSiteLaxMode,
		"weird":  http.SameSiteLaxMode,
	}
	for in, want := range cases {
		if got := sameSiteMode(in); got != want {
			t.Fatalf("sameSiteMode(%q) = %v, want %v", in, got, want)
		}
	}
}

func TestSweepTunnelInvites(t *testing.T) {
	ts := tunnelServer(t) // manual clock at wall=1000
	// Issue an invite that expires at 1010, then advance the clock past it.
	share, _ := ts.srv.deps.TunnelStore.IssueInvites("swp", "s", "c", 1010, 1000, nil)
	ts.srv.clock.(*hub.ManualClock).SetWall(2000)
	ts.srv.sweepTunnelInvites(t.Context())
	if inv := ts.srv.deps.TunnelStore.ConsumeInvite(share, "swp", 2000); inv != nil {
		t.Fatal("sweep should have dropped the expired invite")
	}
}

func TestRequestBaseURLScheme(t *testing.T) {
	plain := httptest.NewRequest("GET", "/x", http.NoBody)
	plain.Host = "plain.example"
	if got := requestBaseURL(plain); got != "http://plain.example" {
		t.Fatalf("http base = %q", got)
	}
	secure := httptest.NewRequest("GET", "/x", http.NoBody)
	secure.Host = "secure.example"
	secure.TLS = &tls.ConnectionState{}
	if got := requestBaseURL(secure); got != "https://secure.example" {
		t.Fatalf("https base = %q", got)
	}
}

// cookieByName returns the named cookie or nil.
func cookieByName(cookies []*http.Cookie, name string) *http.Cookie {
	for _, c := range cookies {
		if c.Name == name {
			return c
		}
	}
	return nil
}

// bodyList decodes a JSON array body into a slice of maps.
func bodyList(t *testing.T, raw []byte) []map[string]any {
	t.Helper()
	var out []map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("decode list: %v (%s)", err, raw)
	}
	return out
}
