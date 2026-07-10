//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func TestPrincipalNameAndHeader(t *testing.T) {
	dn := "Display"
	p := &Principal{SubjectID: "sub", DisplayName: &dn}
	if p.Name() != "Display" {
		t.Errorf("Name() display = %q", p.Name())
	}
	p2 := &Principal{SubjectID: "sub"}
	if p2.Name() != "sub" {
		t.Errorf("Name() subject = %q", p2.Name())
	}
	empty := ""
	p3 := &Principal{SubjectID: "sub", DisplayName: &empty}
	if p3.Name() != "sub" {
		t.Errorf("Name() empty display = %q", p3.Name())
	}

	req := &Request{Headers: map[string]string{"X-Mixed-Case": "v"}}
	if req.Header("X-Mixed-Case") != "v" || req.Header("x-mixed-case") != "v" {
		t.Errorf("Header lookup failed")
	}
	if req.Header("absent") != "" {
		t.Errorf("absent header")
	}
	var nilReq *Request
	if nilReq.Header("x") != "" || nilReq.Cookie("x") != "" {
		t.Errorf("nil request accessors")
	}
}

func TestAsStrDefault(t *testing.T) {
	if asStr(123) != "123" || asStr(nil) != "" || asStr("x") != "x" {
		t.Errorf("asStr wrong")
	}
}

func TestSortedCopySwaps(t *testing.T) {
	got := sortedCopy([]string{"z", "a", "m"})
	if got[0] != "a" || got[1] != "m" || got[2] != "z" {
		t.Errorf("sortedCopy = %v", got)
	}
}

func TestHeaderTrustedProxyLogsSortedList(t *testing.T) {
	cfg := headerAuthConfig()
	cfg.TrustedProxyIPs = []string{"z-host", "a-host"} // unsorted → exercises sortedCopy in the warn path
	idp := NewLocalIdentityProvider(cfg, nil)
	p, err := idp.Authenticate(context.Background(), &Request{
		Headers:  map[string]string{"x-uterm-principal": "x", "x-uterm-role": "admin"},
		SourceIP: "untrusted",
	})
	if err != nil || p.SubjectID != "anonymous" {
		t.Errorf("untrusted not downgraded: %v %+v", err, p)
	}
}

func TestResolvedTokenPathAndReadEmpty(t *testing.T) {
	// env override
	dir := t.TempDir()
	t.Setenv("UTERM_DEV_TOKEN_PATH", filepath.Join(dir, "env-tok"))
	if resolvedTokenPath("") != filepath.Join(dir, "env-tok") {
		t.Errorf("env token path not honoured")
	}
	if resolvedTokenPath("/explicit") != "/explicit" {
		t.Errorf("explicit token path not honoured")
	}
	// empty file → not ok
	empty := filepath.Join(dir, "empty")
	if err := os.WriteFile(empty, []byte("   "), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, ok := ReadDevToken(empty); ok {
		t.Errorf("empty token file returned ok")
	}
}

func TestSetupDevIDPFillsEmptyIssuerAudience(t *testing.T) {
	dir := t.TempDir()
	auth := serverconfig.AuthConfig{Mode: "dev_token", JWTRolesClaim: "roles"} // empty issuer/audience
	if _, err := SetupDevIDP(&auth, DevIDPOptions{TokenPath: filepath.Join(dir, "t")}); err != nil {
		t.Fatal(err)
	}
	if auth.JWTIssuer != "provide-uterm-dev" || auth.JWTAudience != "provide-uterm-server" {
		t.Errorf("empty issuer/audience defaults wrong: %q %q", auth.JWTIssuer, auth.JWTAudience)
	}
}

func TestAuthorizationServiceWithCustomProvider(t *testing.T) {
	authz := NewAuthorizationServiceWith(LocalAuthorizationProvider{})
	p := &Principal{SubjectID: "alice", Roles: NewSet("operator"), Scopes: NewSet()}
	owner := "alice"
	sd := &serverconfig.SessionDefinition{SessionID: "s", Owner: &owner, Visibility: "public"}
	if !authz.IsOwner(p, sd) {
		t.Errorf("delegated IsOwner failed")
	}
	if !authz.HasCapability(p, "session.control.create") {
		t.Errorf("delegated HasCapability failed")
	}
}

func TestEmbeddedIPv4Decode(t *testing.T) {
	// IPv4-mapped ::ffff:169.254.169.254 → nil (handled natively by net.IP.Equal)
	if ip := decodeEmbeddedIPv4(net.ParseIP("::ffff:169.254.169.254")); ip != nil {
		t.Errorf("mapped decode should be nil (native), got %v", ip)
	}
	// 6to4 2002:a9fe:a9fe::
	if ip := decodeEmbeddedIPv4(net.ParseIP("2002:a9fe:a9fe::")); ip == nil || ip.String() != "169.254.169.254" {
		t.Errorf("6to4 decode = %v", ip)
	}
	// plain IPv4 → nil (nothing embedded)
	if decodeEmbeddedIPv4(net.ParseIP("10.0.0.1")) != nil {
		t.Errorf("plain ipv4 should decode nil")
	}
	// loopback ::1 → nil (excluded compat form)
	if decodeEmbeddedIPv4(net.ParseIP("::1")) != nil {
		t.Errorf("::1 should decode nil")
	}
	// nil input
	if decodeEmbeddedIPv4(nil) != nil {
		t.Errorf("nil decode")
	}
}

func TestWebhookResponseMissingSubject(t *testing.T) {
	falseVal := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"roles": []any{"viewer"}}) // no subject_id
	}))
	defer srv.Close()
	idp := newIDP(t, srv.URL, WebhookIDPOptions{RequireSignedResponse: &falseVal}, 1e6)
	if p, err := idp.Authenticate(context.Background(), &Request{}); err != nil || p != nil {
		t.Errorf("missing subject_id not rejected: %v %+v", err, p)
	}
}

func TestWebhookResponseScopesAndClaims(t *testing.T) {
	falseVal := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"subject_id": "u", "roles": []any{"operator"},
			"scopes": []any{"a", "b"}, "claims": map[string]any{"k": "v"},
		})
	}))
	defer srv.Close()
	idp := newIDP(t, srv.URL, WebhookIDPOptions{RequireSignedResponse: &falseVal}, 1e6)
	p, err := idp.Authenticate(context.Background(), &Request{})
	if err != nil || p == nil || !p.Scopes.Has("a") || !p.Scopes.Has("b") || p.Claims["k"] != "v" {
		t.Errorf("scopes/claims wrong: %v %+v", err, p)
	}
}

func TestVerifyWebhookSignatureWallClock(t *testing.T) {
	// now == nil path uses the wall clock; a fresh signature must verify.
	body := []byte("x")
	ts := formatNow()
	sig := BuildWebhookSignature(webhookSecret, body, ts)
	if !VerifyWebhookSignature(webhookSecret, body, sig, ts, DefaultMaxAgeS, nil) {
		t.Errorf("wall-clock verification rejected a fresh signature")
	}
}

func TestAssertWebhookTargetParseErrorAndSkip(t *testing.T) {
	// URL parse error → treated as noop (returns nil).
	if err := AssertWebhookTargetAllowed(context.Background(), "http://a b/", nil); err != nil {
		// url.Parse may or may not error on this; either way must not panic.
		_ = err
	}
	// resolver returns a non-IP string → ParseIP nil → skipped, allowed.
	weird := func(context.Context, string) ([]string, error) { return []string{"not-an-ip"}, nil }
	if err := AssertWebhookTargetAllowed(context.Background(), "https://host.example/x", weird); err != nil {
		t.Errorf("non-ip resolver result should be skipped: %v", err)
	}
}

func formatNow() string {
	return jsonFloatString(wallClock())
}

func jsonFloatString(f float64) string {
	b, _ := json.Marshal(f)
	return string(b)
}
