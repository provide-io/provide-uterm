//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"context"
	"testing"
	"time"

	jwt "github.com/golang-jwt/jwt/v5"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

const testKey = "uterm-test-secret-32-byte-minimum-key"

func makeToken(t *testing.T, sub string, roles []string, expOffset int) string {
	t.Helper()
	now := time.Now().Unix()
	claims := jwt.MapClaims{
		"sub": sub, "roles": roles, "iss": "provide-uterm", "aud": "provide-uterm-server",
		"iat": now, "nbf": now, "exp": now + int64(expOffset),
	}
	tok, err := jwt.NewWithClaims(jwt.SigningMethodHS256, claims).SignedString([]byte(testKey))
	if err != nil {
		t.Fatal(err)
	}
	return tok
}

func jwtAuthConfig() *serverconfig.AuthConfig {
	key := testKey
	return &serverconfig.AuthConfig{
		Mode: "jwt", JWTPublicKeyPEM: &key, JWTAlgorithms: []string{"HS256"},
		JWTIssuer: "provide-uterm", JWTAudience: "provide-uterm-server",
		JWTRolesClaim: "roles", JWTScopesClaim: "scope", ClockSkewSeconds: 15,
		TokenCookie: "uterm_token",
	}
}

func headerAuthConfig() *serverconfig.AuthConfig {
	return &serverconfig.AuthConfig{
		Mode: "header", PrincipalHeader: "x-uterm-principal", RoleHeader: "x-uterm-role",
		PrincipalCookie: "uterm_principal", RoleCookie: "uterm_role",
	}
}

func TestExtractBearerToken(t *testing.T) {
	cases := []struct {
		hdr  string
		want string
	}{
		{"", ""},
		{"Bearer\ttoken", ""}, // split on single space only
		{"   ", ""},           // whitespace-only
		{"Bearer abc", "abc"},
		{"bearer abc", "abc"}, // scheme case-insensitive
		{"Basic abc", ""},     // wrong scheme
		{"Bearer", ""},        // no token part
	}
	for _, tc := range cases {
		req := &Request{Headers: map[string]string{"authorization": tc.hdr}}
		if got := ExtractBearerToken(req); got != tc.want {
			t.Errorf("ExtractBearerToken(%q) = %q, want %q", tc.hdr, got, tc.want)
		}
	}
	// entirely missing authorization key
	if got := ExtractBearerToken(&Request{Headers: map[string]string{}}); got != "" {
		t.Errorf("missing header = %q", got)
	}
}

func TestAnonymousPrincipal(t *testing.T) {
	p := AnonymousPrincipal()
	if p.SubjectID != "anonymous" || !p.Roles.Has("viewer") || len(p.Scopes) != 0 {
		t.Errorf("anonymous wrong: %+v", p)
	}
}

func TestHeaderAuth(t *testing.T) {
	idp := NewLocalIdentityProvider(headerAuthConfig(), nil)

	p := idp.PrincipalFromHeaderAuth(&Request{Headers: map[string]string{"x-uterm-principal": "alice"}})
	if p.SubjectID != "alice" || !p.Roles.Has("viewer") { // missing role → viewer
		t.Errorf("missing role default wrong: %+v", p)
	}
	for _, role := range []string{"viewer", "operator", "admin"} {
		p := idp.PrincipalFromHeaderAuth(&Request{Headers: map[string]string{"x-uterm-principal": "u", "x-uterm-role": role}})
		if !p.Roles.Has(role) {
			t.Errorf("role %q not accepted: %+v", role, p)
		}
	}
	// cookie-provided principal
	p2, err := idp.Authenticate(context.Background(), &Request{Cookies: map[string]string{"uterm_principal": "cookie_user"}})
	if err != nil || p2.SubjectID != "cookie_user" {
		t.Errorf("cookie principal: %v %+v", err, p2)
	}
	// empty scopes
	if len(p.Scopes) != 0 {
		t.Errorf("scopes not empty")
	}
}

func TestHeaderTrustedProxyAllowlistFailClosed(t *testing.T) {
	cfg := headerAuthConfig()
	cfg.TrustedProxyIPs = []string{"10.0.0.5"}
	idp := NewLocalIdentityProvider(cfg, nil)

	// Untrusted source → downgraded to anonymous (fail closed), header ignored.
	untrusted, err := idp.Authenticate(context.Background(), &Request{
		Headers:  map[string]string{"x-uterm-principal": "attacker", "x-uterm-role": "admin"},
		SourceIP: "203.0.113.9",
	})
	if err != nil {
		t.Fatal(err)
	}
	if untrusted.SubjectID != "anonymous" || untrusted.Roles.Has("admin") {
		t.Errorf("untrusted source not fail-closed: %+v", untrusted)
	}

	// Trusted source → header honoured.
	trusted, err := idp.Authenticate(context.Background(), &Request{
		Headers:  map[string]string{"x-uterm-principal": "ops", "x-uterm-role": "admin"},
		SourceIP: "10.0.0.5",
	})
	if err != nil {
		t.Fatal(err)
	}
	if trusted.SubjectID != "ops" || !trusted.Roles.Has("admin") {
		t.Errorf("trusted source not honoured: %+v", trusted)
	}
}

func TestJWTAuth(t *testing.T) {
	idp := NewLocalIdentityProvider(jwtAuthConfig(), nil)
	token := makeToken(t, "user1", []string{"operator"}, 600)

	p, err := idp.Authenticate(context.Background(), &Request{Headers: map[string]string{"authorization": "Bearer " + token}})
	if err != nil {
		t.Fatal(err)
	}
	if p.SubjectID != "user1" || !p.Roles.Has("operator") {
		t.Errorf("jwt principal wrong: %+v", p)
	}

	// No token → anonymous.
	anon, err := idp.Authenticate(context.Background(), &Request{Headers: map[string]string{}})
	if err != nil || anon.SubjectID != "anonymous" {
		t.Errorf("no token: %v %+v", err, anon)
	}

	// Invalid token → anonymous (not error).
	bad, err := idp.Authenticate(context.Background(), &Request{Headers: map[string]string{"authorization": "Bearer invalid.token.here"}})
	if err != nil || bad.SubjectID != "anonymous" {
		t.Errorf("invalid token: %v %+v", err, bad)
	}

	// Token from cookie.
	tok2 := makeToken(t, "cookieuser", []string{"admin"}, 600)
	c, err := idp.Authenticate(context.Background(), &Request{Cookies: map[string]string{"uterm_token": tok2}})
	if err != nil || c.SubjectID != "cookieuser" {
		t.Errorf("cookie token: %v %+v", err, c)
	}
}

func TestJWTRejectsWrongIssuerAndExpired(t *testing.T) {
	idp := NewLocalIdentityProvider(jwtAuthConfig(), nil)

	now := time.Now().Unix()
	wrongIss, _ := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
		"sub": "a", "roles": []string{"admin"}, "iss": "wrong", "aud": "provide-uterm-server",
		"iat": now, "exp": now + 600,
	}).SignedString([]byte(testKey))
	if _, err := idp.PrincipalFromJWTToken(wrongIss); err == nil {
		t.Error("wrong issuer accepted")
	}

	expired := makeToken(t, "a", []string{"admin"}, -600)
	if _, err := idp.PrincipalFromJWTToken(expired); err == nil {
		t.Error("expired token accepted")
	}

	// 'none' alg rejected (algorithms list excludes it).
	noneTok := "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJhIiwiaXNzIjoicHJvdmlkZS11dGVybSIsImF1ZCI6InByb3ZpZGUtdXRlcm0tc2VydmVyIiwiZXhwIjo0ODUzNjAwMDAwfQ." // pragma: allowlist secret
	if _, err := idp.PrincipalFromJWTToken(noneTok); err == nil {
		t.Error("'none' alg token accepted")
	}
}

func TestJWTNoOptionalClaims(t *testing.T) {
	idp := NewLocalIdentityProvider(jwtAuthConfig(), nil)
	now := time.Now().Unix()
	// Only sub + exp (no iat/nbf) must still validate.
	tok, _ := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
		"sub": "alice", "roles": []string{"admin"}, "iss": "provide-uterm",
		"aud": "provide-uterm-server", "exp": now + 600,
	}).SignedString([]byte(testKey))
	p, err := idp.PrincipalFromJWTToken(tok)
	if err != nil || p.SubjectID != "alice" {
		t.Fatalf("no-optional-claims token: %v %+v", err, p)
	}
}

func TestRolesFromClaims(t *testing.T) {
	idp := NewLocalIdentityProvider(jwtAuthConfig(), nil)
	// list branch
	r := idp.rolesFromClaims(jwt.MapClaims{"roles": []any{"viewer", "operator"}})
	if !r.Has("viewer") || !r.Has("operator") {
		t.Errorf("list roles = %v", r.Sorted())
	}
	// empty strings filtered
	r2 := idp.rolesFromClaims(jwt.MapClaims{"roles": []any{"", "admin", ""}})
	if !r2.Has("admin") || r2.Has("") {
		t.Errorf("filtered roles = %v", r2.Sorted())
	}
	// string branch (comma/space split)
	r3 := idp.rolesFromClaims(jwt.MapClaims{"roles": "admin, operator"})
	if !r3.Has("admin") || !r3.Has("operator") {
		t.Errorf("string roles = %v", r3.Sorted())
	}
	// unknown roles dropped, fall back to viewer
	r4 := idp.rolesFromClaims(jwt.MapClaims{"roles": []any{"superuser"}})
	if !r4.Has("viewer") || len(r4) != 1 {
		t.Errorf("unknown roles fallback = %v", r4.Sorted())
	}
}

func TestScopesFromClaims(t *testing.T) {
	idp := NewLocalIdentityProvider(jwtAuthConfig(), nil)
	s := idp.scopesFromClaims(jwt.MapClaims{"scope": "read write"})
	if !s.Has("read") || !s.Has("write") {
		t.Errorf("string scopes = %v", s.Sorted())
	}
	s2 := idp.scopesFromClaims(jwt.MapClaims{"scope": []any{"a", "b"}})
	if !s2.Has("a") || !s2.Has("b") {
		t.Errorf("list scopes = %v", s2.Sorted())
	}
}

func TestUnknownModeErrors(t *testing.T) {
	cfg := &serverconfig.AuthConfig{Mode: "dev_token"}
	idp := NewLocalIdentityProvider(cfg, nil)
	if _, err := idp.Authenticate(context.Background(), &Request{}); err == nil {
		t.Error("dev_token (unset-up) mode should error at Authenticate")
	}
}

func TestFilterKnownRolesCaseFolds(t *testing.T) {
	for _, in := range [][]string{{"Admin"}, {"ADMIN"}, {"aDmIn"}, {"  Admin  "}} {
		got := FilterKnownRoles(in)
		if len(got) != 1 || !got.Has("admin") {
			t.Errorf("FilterKnownRoles(%v) = %v", in, got.Sorted())
		}
	}
}

// TestJWTFromCFAccessJWTAssertionHeader: CF-Access-JWT-Assertion supplies
// verifiable JWT material (HS256 test key) and maps subject — never via the
// unsigned email header.
func TestJWTFromCFAccessJWTAssertionHeader(t *testing.T) {
	idp := NewLocalIdentityProvider(jwtAuthConfig(), nil)
	token := makeToken(t, "cf-user@example.com", []string{"operator"}, 600)

	p, err := idp.Authenticate(context.Background(), &Request{
		Headers: map[string]string{"CF-Access-JWT-Assertion": token},
	})
	if err != nil {
		t.Fatal(err)
	}
	if p.SubjectID != "cf-user@example.com" || !p.Roles.Has("operator") {
		t.Errorf("CF-Access-JWT-Assertion principal wrong: %+v", p)
	}

	// Case-insensitive header name (HTTP canonical form).
	p2, err := idp.Authenticate(context.Background(), &Request{
		Headers: map[string]string{"cf-access-jwt-assertion": token},
	})
	if err != nil || p2.SubjectID != "cf-user@example.com" {
		t.Errorf("case-insensitive CF-Access-JWT-Assertion: %v %+v", err, p2)
	}
}

func TestJWTFromCFAuthorizationCookie(t *testing.T) {
	idp := NewLocalIdentityProvider(jwtAuthConfig(), nil)
	token := makeToken(t, "cookie-cf-user", []string{"admin"}, 600)

	p, err := idp.Authenticate(context.Background(), &Request{
		Cookies: map[string]string{"CF_Authorization": token},
	})
	if err != nil {
		t.Fatal(err)
	}
	if p.SubjectID != "cookie-cf-user" || !p.Roles.Has("admin") {
		t.Errorf("CF_Authorization cookie principal wrong: %+v", p)
	}
}

// TestCFAccessEmailHeaderAloneDoesNotAuthenticate: spoofable Access email
// must not mint identity in jwt mode.
func TestCFAccessEmailHeaderAloneDoesNotAuthenticate(t *testing.T) {
	idp := NewLocalIdentityProvider(jwtAuthConfig(), nil)
	p, err := idp.Authenticate(context.Background(), &Request{
		Headers: map[string]string{
			"Cf-Access-Authenticated-User-Email": "spoofed@evil.example",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if p.SubjectID != "anonymous" {
		t.Errorf("email header alone authenticated as %q", p.SubjectID)
	}
}

func TestJWTTokenSourcePrecedence(t *testing.T) {
	idp := NewLocalIdentityProvider(jwtAuthConfig(), nil)
	bearer := makeToken(t, "from-bearer", []string{"admin"}, 600)
	cfHdr := makeToken(t, "from-cf-hdr", []string{"admin"}, 600)
	cfCookie := makeToken(t, "from-cf-cookie", []string{"admin"}, 600)
	appCookie := makeToken(t, "from-app-cookie", []string{"admin"}, 600)

	// Bearer wins over CF-Access-JWT-Assertion.
	p, err := idp.Authenticate(context.Background(), &Request{
		Headers: map[string]string{
			"authorization":           "Bearer " + bearer,
			"CF-Access-JWT-Assertion": cfHdr,
		},
		Cookies: map[string]string{
			"CF_Authorization": cfCookie,
			"uterm_token":      appCookie,
		},
	})
	if err != nil || p.SubjectID != "from-bearer" {
		t.Errorf("bearer precedence: %v %+v", err, p)
	}

	// CF-Access-JWT-Assertion wins over CF_Authorization cookie.
	p, err = idp.Authenticate(context.Background(), &Request{
		Headers: map[string]string{"CF-Access-JWT-Assertion": cfHdr},
		Cookies: map[string]string{
			"CF_Authorization": cfCookie,
			"uterm_token":      appCookie,
		},
	})
	if err != nil || p.SubjectID != "from-cf-hdr" {
		t.Errorf("CF-Access-JWT-Assertion precedence: %v %+v", err, p)
	}

	// CF_Authorization wins over Auth.TokenCookie.
	p, err = idp.Authenticate(context.Background(), &Request{
		Cookies: map[string]string{
			"CF_Authorization": cfCookie,
			"uterm_token":      appCookie,
		},
	})
	if err != nil || p.SubjectID != "from-cf-cookie" {
		t.Errorf("CF_Authorization precedence: %v %+v", err, p)
	}
}

func TestJWTDefaultRoleWhenNoRolesClaim(t *testing.T) {
	cfg := jwtAuthConfig()
	cfg.JwtDefaultRole = "operator"
	idp := NewLocalIdentityProvider(cfg, nil)

	now := time.Now().Unix()
	// No roles claim — CF Access style.
	tok, err := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
		"sub": "user@example.com", "iss": "provide-uterm", "aud": "provide-uterm-server",
		"iat": now, "exp": now + 600,
	}).SignedString([]byte(testKey))
	if err != nil {
		t.Fatal(err)
	}
	p, err := idp.PrincipalFromJWTToken(tok)
	if err != nil {
		t.Fatal(err)
	}
	if p.SubjectID != "user@example.com" || !p.Roles.Has("operator") || len(p.Roles) != 1 {
		t.Errorf("jwt_default_role not applied: %+v roles=%v", p, p.Roles.Sorted())
	}

	// Explicit roles claim still wins — default must not replace claim roles.
	tok2 := makeToken(t, "u2", []string{"admin"}, 600)
	p2, err := idp.PrincipalFromJWTToken(tok2)
	if err != nil {
		t.Fatal(err)
	}
	if !p2.Roles.Has("admin") || len(p2.Roles) != 1 {
		t.Errorf("default should not override claim roles: %v", p2.Roles.Sorted())
	}
}

func TestJWTDefaultRoleUnknownFallsBackToViewer(t *testing.T) {
	cfg := jwtAuthConfig()
	cfg.JwtDefaultRole = "superuser" // not in known roles
	idp := NewLocalIdentityProvider(cfg, nil)
	r := idp.rolesFromClaims(jwt.MapClaims{})
	if !r.Has("viewer") || len(r) != 1 {
		t.Errorf("unknown jwt_default_role should filter to viewer, got %v", r.Sorted())
	}
}
