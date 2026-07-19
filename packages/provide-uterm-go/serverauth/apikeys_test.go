//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"context"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func TestApiKeyStoreCRUD(t *testing.T) {
	store := NewApiKeyStore()
	raw, rec := store.Create("test-key", nil, nil)
	if len(raw) <= 20 {
		t.Errorf("raw key too short")
	}
	if rec.KeyID != HashKey(raw)[:16] || rec.KeyHash != HashKey(raw) || rec.Revoked || len(rec.Scopes) != 0 {
		t.Errorf("record wrong: %+v", rec)
	}
	if rec.ExpiresAt != nil {
		t.Errorf("expires_at should be nil")
	}

	got := store.Validate(raw)
	if got == nil || got.KeyID != rec.KeyID || got.LastUsedAt == nil {
		t.Errorf("validate correct key: %+v", got)
	}
	if store.Validate("wrong-key-value") != nil {
		t.Errorf("wrong key validated")
	}

	if !store.Revoke(rec.KeyID) || store.Validate(raw) != nil {
		t.Errorf("revoke failed")
	}
	if store.Revoke("nonexistent") {
		t.Errorf("revoke unknown returned true")
	}
}

func TestApiKeyExpiry(t *testing.T) {
	store := NewApiKeyStore()
	fake := 1000.0
	store.SetClock(func() float64 { return fake })
	exp := 1
	raw, _ := store.Create("k", nil, &exp)
	if store.Validate(raw) == nil {
		t.Errorf("not-yet-expired key rejected")
	}
	fake = 1000.0 + 3600
	if store.Validate(raw) != nil {
		t.Errorf("expired key accepted")
	}
}

func TestApiKeyScopeRoleMapping(t *testing.T) {
	cfg := &serverconfig.AuthConfig{APIKeysEnabled: true, Mode: "dev_token"}
	cases := []struct {
		scopes   Set
		wantRole string
		wantNil  bool
	}{
		{NewSet(), "", true},                // empty scopes rejected
		{NewSet("session.read"), "", true},  // capability-only scope rejected
		{NewSet("administrator"), "", true}, // typo rejected
		{NewSet("admin"), "admin", false},
		{NewSet("operator"), "operator", false},
		{NewSet("viewer"), "viewer", false},
	}
	for _, tc := range cases {
		store := NewApiKeyStore()
		raw, _, err := store.CreateForTenant("acme", "k", tc.scopes, nil)
		if err != nil {
			t.Fatalf("CreateForTenant: %v", err)
		}
		idp := NewLocalIdentityProvider(cfg, store)
		p := idp.PrincipalFromAPIKey(&Request{Headers: map[string]string{"x-api-key": raw}})
		if tc.wantNil {
			if p != nil {
				t.Errorf("scopes %v: expected nil, got %+v", tc.scopes.Sorted(), p)
			}
			continue
		}
		if p == nil || !p.Roles.Has(tc.wantRole) || !p.Scopes.Has("*") {
			t.Errorf("scopes %v: got %+v, want role %q", tc.scopes.Sorted(), p, tc.wantRole)
		}
		if p != nil && (p.TenantID == nil || *p.TenantID != "acme") {
			t.Errorf("scopes %v: tenant not propagated: %+v", tc.scopes.Sorted(), p.TenantID)
		}
	}
}

func TestApiKeyDisabledAndPrecedence(t *testing.T) {
	store := NewApiKeyStore()
	raw, _, err := store.CreateForTenant("acme", "k", NewSet("admin"), nil)
	if err != nil {
		t.Fatalf("CreateForTenant: %v", err)
	}

	disabled := &serverconfig.AuthConfig{APIKeysEnabled: false, Mode: "dev_token"}
	idp := NewLocalIdentityProvider(disabled, store)
	if idp.PrincipalFromAPIKey(&Request{Headers: map[string]string{"x-api-key": raw}}) != nil {
		t.Errorf("disabled api keys still resolved")
	}

	enabled := &serverconfig.AuthConfig{APIKeysEnabled: true, Mode: "header"}
	idp2 := NewLocalIdentityProvider(enabled, store)
	// api key takes precedence over the header mode.
	p, err := idp2.Authenticate(context.Background(), &Request{Headers: map[string]string{"x-api-key": raw}})
	if err != nil || p == nil || !p.Roles.Has("admin") {
		t.Errorf("api key precedence: %v %+v", err, p)
	}

	// no store → nil
	idp3 := NewLocalIdentityProvider(enabled, nil)
	if idp3.PrincipalFromAPIKey(&Request{Headers: map[string]string{"x-api-key": raw}}) != nil {
		t.Errorf("nil store resolved a key")
	}
	// empty header → nil
	if idp2.PrincipalFromAPIKey(&Request{Headers: map[string]string{"x-api-key": ""}}) != nil {
		t.Errorf("empty api key header resolved")
	}
	// per-store isolation
	other := NewApiKeyStore()
	idp4 := NewLocalIdentityProvider(enabled, other)
	if idp4.PrincipalFromAPIKey(&Request{Headers: map[string]string{"x-api-key": raw}}) != nil {
		t.Errorf("key valid under a different store")
	}
}

func TestApiKeyListKeys(t *testing.T) {
	store := NewApiKeyStore()
	store.Create("a", nil, nil)
	store.Create("b", nil, nil)
	if len(store.ListKeys()) != 2 {
		t.Errorf("list_keys wrong")
	}
}
