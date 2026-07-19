//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"testing"
	"time"

	jwt "github.com/golang-jwt/jwt/v5"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func TestCanonicalTenantID(t *testing.T) {
	cases := []struct {
		in   string
		want *string
	}{
		{"", nil},
		{"   ", nil},
		{"bad tenant!", nil},
		{"-leading", nil},
		{"acme", strPtr("acme")},
		{"  acme  ", strPtr("acme")},
		{"a.b-c_9", strPtr("a.b-c_9")},
	}
	for _, tc := range cases {
		got := CanonicalTenantID(tc.in)
		switch {
		case tc.want == nil && got != nil:
			t.Errorf("CanonicalTenantID(%q) = %q, want nil", tc.in, *got)
		case tc.want != nil && (got == nil || *got != *tc.want):
			t.Errorf("CanonicalTenantID(%q) = %v, want %q", tc.in, got, *tc.want)
		}
	}
}

func strPtr(s string) *string { return &s }

func TestApiKeyTenantScoping(t *testing.T) {
	store := NewApiKeyStore()

	// Invalid tenant is rejected.
	if _, _, err := store.CreateForTenant("bad tenant!", "k", NewSet("admin"), nil); err == nil {
		t.Fatalf("expected error for invalid tenant")
	}
	if _, _, err := store.CreateForTenant("", "k", NewSet("admin"), nil); err == nil {
		t.Fatalf("expected error for empty tenant")
	}

	_, recA, err := store.CreateForTenant("acme", "ka", NewSet("admin"), nil)
	if err != nil {
		t.Fatalf("CreateForTenant acme: %v", err)
	}
	if recA.TenantID != "acme" {
		t.Fatalf("tenant not stored: %q", recA.TenantID)
	}
	_, recB, err := store.CreateForTenant("beta", "kb", NewSet("viewer"), nil)
	if err != nil {
		t.Fatalf("CreateForTenant beta: %v", err)
	}

	// ListKeysForTenant is tenant-scoped.
	if got := store.ListKeysForTenant("acme"); len(got) != 1 || got[0].KeyID != recA.KeyID {
		t.Fatalf("acme list wrong: %+v", got)
	}
	if got := store.ListKeysForTenant("beta"); len(got) != 1 || got[0].KeyID != recB.KeyID {
		t.Fatalf("beta list wrong: %+v", got)
	}
	// Invalid tenant → empty.
	if got := store.ListKeysForTenant("bad tenant!"); len(got) != 0 {
		t.Fatalf("invalid tenant list not empty: %+v", got)
	}

	// RevokeForTenant only affects the owning tenant.
	if store.RevokeForTenant(recA.KeyID, "beta") {
		t.Fatalf("cross-tenant revoke succeeded")
	}
	if store.RevokeForTenant(recA.KeyID, "bad tenant!") {
		t.Fatalf("invalid-tenant revoke succeeded")
	}
	if store.RevokeForTenant("nope", "acme") {
		t.Fatalf("unknown key revoke succeeded")
	}
	if !store.RevokeForTenant(recA.KeyID, "acme") {
		t.Fatalf("own revoke failed")
	}
	// Revoked keys drop out of the tenant listing.
	if got := store.ListKeysForTenant("acme"); len(got) != 0 {
		t.Fatalf("revoked key still listed: %+v", got)
	}
}

func TestAPIKeyTenantResolutionFailsClosed(t *testing.T) {
	cfg := &serverconfig.AuthConfig{APIKeysEnabled: true, Mode: "header"}
	store := NewApiKeyStore()

	// A legacy flat-Create key carries no tenant → rejected (fail closed).
	rawNoTenant, _ := store.Create("legacy", NewSet("admin"), nil)
	idp := NewLocalIdentityProvider(cfg, store)
	if p := idp.PrincipalFromAPIKey(&Request{Headers: map[string]string{"x-api-key": rawNoTenant}}); p != nil {
		t.Fatalf("tenant-less key resolved a principal: %+v", p)
	}

	// A tenant-scoped key resolves and carries the tenant.
	rawTenant, _, err := store.CreateForTenant("acme", "scoped", NewSet("operator"), nil)
	if err != nil {
		t.Fatalf("CreateForTenant: %v", err)
	}
	p := idp.PrincipalFromAPIKey(&Request{Headers: map[string]string{"x-api-key": rawTenant}})
	if p == nil || p.TenantID == nil || *p.TenantID != "acme" || !p.Roles.Has("operator") {
		t.Fatalf("tenant key principal wrong: %+v", p)
	}
}

func TestHeaderTenantResolution(t *testing.T) {
	cfg := headerAuthConfig()
	cfg.TenantHeader = "x-uterm-tenant"
	cfg.TenantCookie = "uterm_tenant"
	idp := NewLocalIdentityProvider(cfg, nil)

	// Valid tenant header.
	p := idp.PrincipalFromHeaderAuth(&Request{Headers: map[string]string{
		"x-uterm-principal": "alice", "x-uterm-role": "operator", "x-uterm-tenant": "acme",
	}})
	if p.TenantID == nil || *p.TenantID != "acme" {
		t.Fatalf("tenant not resolved: %+v", p.TenantID)
	}

	// No tenant → nil (allowed).
	p = idp.PrincipalFromHeaderAuth(&Request{Headers: map[string]string{"x-uterm-principal": "alice"}})
	if p.TenantID != nil {
		t.Fatalf("expected nil tenant, got %v", *p.TenantID)
	}

	// Invalid tenant → fails closed to anonymous.
	p = idp.PrincipalFromHeaderAuth(&Request{Headers: map[string]string{
		"x-uterm-principal": "alice", "x-uterm-tenant": "bad tenant!",
	}})
	if p.SubjectID != "anonymous" || p.TenantID != nil {
		t.Fatalf("invalid tenant did not fail closed: %+v", p)
	}
}

func tenantJWTConfig() *serverconfig.AuthConfig {
	c := jwtAuthConfig()
	c.JWTTenantClaim = "tenant_id"
	return c
}

func makeTenantToken(t *testing.T, sub, tenant string) string {
	t.Helper()
	now := time.Now().Unix()
	claims := jwt.MapClaims{
		"sub": sub, "roles": []string{"operator"}, "iss": "provide-uterm",
		"aud": "provide-uterm-server", "iat": now, "exp": now + 3600,
	}
	if tenant != "" {
		claims["tenant_id"] = tenant
	}
	tok, err := jwt.NewWithClaims(jwt.SigningMethodHS256, claims).SignedString([]byte(testKey))
	if err != nil {
		t.Fatal(err)
	}
	return tok
}

func TestJWTTenantResolution(t *testing.T) {
	idp := NewLocalIdentityProvider(tenantJWTConfig(), nil)

	p, err := idp.PrincipalFromJWTToken(makeTenantToken(t, "alice", "acme"))
	if err != nil {
		t.Fatalf("valid tenant token: %v", err)
	}
	if p.TenantID == nil || *p.TenantID != "acme" {
		t.Fatalf("tenant claim not resolved: %+v", p.TenantID)
	}

	// No tenant claim → nil tenant, still valid.
	p, err = idp.PrincipalFromJWTToken(makeTenantToken(t, "alice", ""))
	if err != nil || p.TenantID != nil {
		t.Fatalf("no-tenant token: %v %+v", err, p)
	}

	// Invalid tenant claim → rejected.
	if _, err := idp.PrincipalFromJWTToken(makeTenantToken(t, "alice", "bad tenant!")); err == nil {
		t.Fatalf("invalid tenant claim accepted")
	}
}

func TestDevIDPTenantClaim(t *testing.T) {
	dir := t.TempDir()
	auth := *serverconfig.DefaultServerConfig()
	a := auth.Auth
	token, err := SetupDevIDP(&a, DevIDPOptions{
		TokenPath: dir + "/tok", Subject: "dev", Roles: []string{"operator"}, Tenant: "acme",
	})
	if err != nil {
		t.Fatalf("SetupDevIDP: %v", err)
	}
	idp := NewLocalIdentityProvider(&a, nil)
	p, err := idp.PrincipalFromJWTToken(token)
	if err != nil {
		t.Fatalf("validate dev token: %v", err)
	}
	if p.TenantID == nil || *p.TenantID != "acme" {
		t.Fatalf("dev token tenant not resolved: %+v", p.TenantID)
	}
}
