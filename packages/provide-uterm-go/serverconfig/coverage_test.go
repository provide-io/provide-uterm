//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"strings"
	"testing"
)

func sp(s string) *string { return &s }

// TestIsLoopbackHost pins the exact three-name set (not net.IP.IsLoopback):
// case/whitespace-insensitive on the three literal names, and NOT loopback for
// anything else — including addresses IsLoopback would accept (127.0.0.2) and
// hostnames that only resolve to loopback (localhost.example is not the
// literal "localhost").
func TestIsLoopbackHost(t *testing.T) {
	cases := []struct {
		host string
		want bool
	}{
		{"localhost", true},
		{"127.0.0.1", true},
		{"::1", true},
		{" Localhost ", true}, // case/whitespace-insensitive
		{"127.0.0.2", false},  // IsLoopback would say true; this set deliberately does not
		{"0:0:0:0:0:0:0:1", false},
		{"localhost.example", false},
		{"example.com", false},
		{"", false},
	}
	for _, c := range cases {
		if got := IsLoopbackHost(c.host); got != c.want {
			t.Errorf("IsLoopbackHost(%q) = %v, want %v", c.host, got, c.want)
		}
	}
}

func TestRequireSecureURL(t *testing.T) {
	field := "auth.webhook_idp_url"
	ok := []*string{nil, sp(""), sp("https://example.com/jwks"),
		sp("http://127.0.0.1:8080/cb"), sp("http://localhost/cb"), sp("http://api.localhost/cb")}
	for _, u := range ok {
		if err := requireSecureURL(u, field); err != nil {
			t.Errorf("requireSecureURL(%v) = %v, want nil", u, err)
		}
	}
	if err := requireSecureURL(sp("ftp://files.example.com"), field); err == nil || !strings.Contains(err.Error(), "must use http(s)") {
		t.Errorf("ftp scheme: %v", err)
	}
	if err := requireSecureURL(sp("http://evil.example.com/steal"), field); err == nil || !strings.Contains(err.Error(), "must use https://") {
		t.Errorf("routable http: %v", err)
	}
	// parse failure → treated as scheme error
	if err := requireSecureURL(sp("http://a b c"), field); err == nil {
		t.Errorf("malformed url accepted")
	}
}

func TestValidateAuthBranches(t *testing.T) {
	cases := []struct {
		mut  func(*AuthConfig)
		want string
	}{
		{func(a *AuthConfig) { a.IdentityProvider = "bogus" }, "identity_provider"},
		{func(a *AuthConfig) { a.WebhookIDPOnFailure = "bogus" }, "webhook_idp_on_failure"},
		{func(a *AuthConfig) { a.RequireUpstreamProxySecret = true }, "upstream_proxy_secret is required"}, // pragma: allowlist secret
		{func(a *AuthConfig) { a.WebhookIDPURL = sp("http://evil.com/x") }, "must use https://"},
		{func(a *AuthConfig) { a.JWTJWKSURL = sp("http://evil.com/x") }, "must use https://"},
		{func(a *AuthConfig) {
			a.IdentityProvider = "webhook"
			a.WebhookIDPRequireSignedResponse = true
			a.WebhookIDPSecret = nil // pragma: allowlist secret
			a.WebhookIDPURL = sp("https://idp.example.com")
		}, "needs auth.webhook_idp_secret"},
	}
	for _, tc := range cases {
		a := defaultAuthConfig()
		tc.mut(&a)
		if err := validateAuth(&a); err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("want %q, got %v", tc.want, err)
		}
	}
	// valid webhook config with secret passes.
	a := defaultAuthConfig()
	a.IdentityProvider = "webhook"
	a.WebhookIDPSecret = sp("s")
	a.WebhookIDPURL = sp("https://idp.example.com")
	if err := validateAuth(&a); err != nil {
		t.Errorf("valid webhook auth: %v", err)
	}
	// proxy secret provided ok.
	a2 := defaultAuthConfig()
	a2.RequireUpstreamProxySecret = true // pragma: allowlist secret
	a2.UpstreamProxySecret = sp("secret")
	if err := validateAuth(&a2); err != nil {
		t.Errorf("proxy secret provided: %v", err)
	}
}

func TestCfAccessTeamDomainAutoFill(t *testing.T) {
	a := defaultAuthConfig()
	a.CfAccessTeamDomain = "myteam"
	a.JWTIssuer = "" // empty so auto-fill applies
	a.JWTJWKSURL = nil
	if err := validateAuth(&a); err != nil {
		t.Fatalf("validateAuth: %v", err)
	}
	wantJWKS := "https://myteam.cloudflareaccess.com/cdn-cgi/access/certs"
	wantIss := "https://myteam.cloudflareaccess.com"
	if a.JWTJWKSURL == nil || *a.JWTJWKSURL != wantJWKS {
		t.Errorf("jwks = %v, want %q", a.JWTJWKSURL, wantJWKS)
	}
	if a.JWTIssuer != wantIss {
		t.Errorf("issuer = %q, want %q", a.JWTIssuer, wantIss)
	}

	// Explicit values win.
	a2 := defaultAuthConfig()
	a2.CfAccessTeamDomain = "myteam"
	a2.JWTIssuer = "https://custom.example"
	explicit := "https://custom.example/jwks"
	a2.JWTJWKSURL = &explicit
	if err := validateAuth(&a2); err != nil {
		t.Fatalf("validateAuth explicit: %v", err)
	}
	if a2.JWTIssuer != "https://custom.example" || *a2.JWTJWKSURL != explicit {
		t.Errorf("explicit values overridden: iss=%q jwks=%v", a2.JWTIssuer, a2.JWTJWKSURL)
	}

	// Scheme/path stripped from team domain.
	a3 := defaultAuthConfig()
	a3.CfAccessTeamDomain = "https://other.cloudflareaccess.com/"
	a3.JWTIssuer = ""
	if err := validateAuth(&a3); err != nil {
		t.Fatalf("validateAuth scheme: %v", err)
	}
	if a3.JWTIssuer != "https://other.cloudflareaccess.com" {
		t.Errorf("scheme strip issuer = %q", a3.JWTIssuer)
	}
}

func TestValidateMiscSections(t *testing.T) {
	if err := validateAudit(&AuditConfig{ChainEnabled: true}); err == nil {
		t.Errorf("audit chain without file accepted")
	}
	if err := validateAudit(&AuditConfig{ChainEnabled: true, ChainFile: sp("/x.jsonl")}); err != nil {
		t.Errorf("audit chain with file: %v", err)
	}
	if err := validateSecurity(&SecurityConfig{Mode: "bogus", DefaultSessionVisibility: "public"}); err == nil {
		t.Errorf("bad security.mode accepted")
	}
	if err := validateSecurity(&SecurityConfig{Mode: "strict", DefaultSessionVisibility: "bogus"}); err == nil {
		t.Errorf("bad default_session_visibility accepted")
	}
	if err := validateTunnel(&TunnelConfig{TokenTransport: "bogus", CookieSamesite: "lax", TokenTTLS: 3600}); err == nil {
		t.Errorf("bad token_transport accepted")
	}
	if err := validateTunnel(&TunnelConfig{TokenTransport: "cookie", CookieSamesite: "bogus", TokenTTLS: 3600}); err == nil {
		t.Errorf("bad cookie_samesite accepted")
	}
	if err := validatePam(&PamConfig{Mode: "bogus"}); err == nil {
		t.Errorf("bad pam.mode accepted")
	}
	if err := validatePam(&PamConfig{Mode: "notify", RelayURL: sp("http://evil.com/x")}); err == nil {
		t.Errorf("insecure pam.relay_url accepted")
	}
	if err := validateControlPlane(&ControlPlaneConfig{Backend: "bogus", ReapIntervalS: 1, ReapRetentionS: 0}); err == nil {
		t.Errorf("bad control_plane.backend accepted")
	}
}

func TestValidateGovernanceURLs(t *testing.T) {
	fields := []func(*GovernanceConfig){
		func(g *GovernanceConfig) { g.PolicyWebhookURL = sp("http://evil.com/x") },
		func(g *GovernanceConfig) { g.RegistryWebhookURL = sp("http://evil.com/x") },
		func(g *GovernanceConfig) { g.AuthzWebhookURL = sp("http://evil.com/x") },
		func(g *GovernanceConfig) { g.BehavioralAuditURL = sp("http://evil.com/x") },
		func(g *GovernanceConfig) { g.TelemetryWebhookURL = sp("http://evil.com/x") },
	}
	for i, mut := range fields {
		g := defaultGovernanceConfig()
		mut(&g)
		if err := validateGovernance(&g); err == nil {
			t.Errorf("governance url %d not validated", i)
		}
	}
	g := defaultGovernanceConfig()
	if err := validateGovernance(&g); err != nil {
		t.Errorf("default governance: %v", err)
	}
}

func TestTopScalars(t *testing.T) {
	c := mustConfig(t, map[string]any{
		"environment":                   "dev",
		"session_idle_timeout_s":        int64(120),
		"session_retention_s":           int64(3600),
		"browser_rate_limit_per_sec":    50.0,
		"worker_frame_on_invalid":       "reject",
		"max_connections_per_principal": int64(5),
		"max_workers":                   int64(7),
	})
	if c.Environment != "dev" || c.SessionIdleTimeoutS != 120 || c.SessionRetentionS != 3600 ||
		c.BrowserRateLimitPerSec != 50.0 || c.WorkerFrameOnInvalid != "reject" ||
		c.MaxConnectionsPerPrincipal != 5 || c.MaxWorkers != 7 {
		t.Errorf("top scalars wrong: %+v", c)
	}

	for _, tc := range []struct {
		data map[string]any
		want string
	}{
		{map[string]any{"environment": "bogus"}, "environment"},
		{map[string]any{"worker_frame_on_invalid": "bogus"}, "worker_frame_on_invalid"},
		{map[string]any{"max_workers": int64(0)}, "max_workers must be >= 1"},
	} {
		if _, err := ConfigFromMapping(tc.data); err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("want %q, got %v", tc.want, err)
		}
	}
}

func TestSessionEdgeCases(t *testing.T) {
	// keystroke_queue valid + invalid
	if c := mustConfig(t, map[string]any{"sessions": []any{map[string]any{
		"session_id": "s", "connector_type": "shell", "keystroke_queue": "replay",
		"presence": true, "auto_transfer_idle_s": int64(10), "owner": "alice",
		"recording_enabled": true, "input_mode": "hijack", "visibility": "private",
	}}}); c.Sessions[0].KeystrokeQueue != "replay" || !c.Sessions[0].Presence ||
		c.Sessions[0].AutoTransferIdleS != 10 || c.Sessions[0].Owner == nil ||
		c.Sessions[0].RecordingEnabled == nil || c.Sessions[0].InputMode != "hijack" ||
		c.Sessions[0].Visibility != "private" {
		t.Errorf("session fields wrong: %+v", c.Sessions[0])
	}
	if _, err := ConfigFromMapping(map[string]any{"sessions": []any{map[string]any{
		"session_id": "s", "connector_type": "shell", "keystroke_queue": "bogus"}}}); err == nil {
		t.Errorf("bad keystroke_queue accepted")
	}
	if _, err := ConfigFromMapping(map[string]any{"sessions": []any{map[string]any{
		"session_id": "s", "connector_type": "bogus"}}}); err == nil {
		t.Errorf("bogus connector_type accepted")
	}
	// sessions not a list
	if _, err := ConfigFromMapping(map[string]any{"sessions": "not-a-list"}); err == nil {
		t.Errorf("non-list sessions accepted")
	}
}

func TestLoadServerConfigPaths(t *testing.T) {
	if c, err := LoadServerConfig(""); err != nil || c.Auth.Mode != "dev_token" {
		t.Errorf("empty path default: %v", err)
	}
	if _, err := LoadServerConfig("/nonexistent/xyz.toml"); err == nil {
		t.Errorf("missing file accepted")
	}
}

func TestHelpers(t *testing.T) {
	if asString(nil) != "" || asString(true) != "True" || asString(false) != "False" || asString(int64(3)) != "3" {
		t.Errorf("asString wrong")
	}
	if strOr(nil, "fb") != "fb" || strOr("x", "fb") != "x" || strOr(int64(2), "fb") != "2" {
		t.Errorf("strOr wrong")
	}
	if v, ok := asInt(int(3)); !ok || v != 3 {
		t.Errorf("asInt int")
	}
	if v, ok := asInt(float64(4)); !ok || v != 4 {
		t.Errorf("asInt float")
	}
	if _, ok := asInt("x"); ok {
		t.Errorf("asInt string ok")
	}
	if asFloat(int64(5)) != 5 || asFloat(int(6)) != 6 || asFloat(float64(7)) != 7 || asFloat("x") != 0 {
		t.Errorf("asFloat wrong")
	}
	if s := asStringSlice([]string{"a"}); len(s) != 1 || s[0] != "a" {
		t.Errorf("asStringSlice []string")
	}
	if asStringSlice("x") != nil {
		t.Errorf("asStringSlice non-list")
	}
	if pyTypeName("x") != "str" || pyTypeName(true) != "bool" || pyTypeName(int64(1)) != "int" ||
		pyTypeName(1.0) != "float" || pyTypeName(map[string]any{}) != "dict" || pyTypeName(nil) != "NoneType" {
		t.Errorf("pyTypeName wrong")
	}
}
