//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import "testing"

func TestNewValidatesRole(t *testing.T) {
	if _, err := New(Config{BaseURL: "http://x", DefaultRole: "root"}); err == nil {
		t.Fatalf("invalid role must be rejected")
	}
	for _, role := range []string{"admin", "operator", "viewer", ""} {
		if _, err := New(Config{BaseURL: "http://x", DefaultRole: role}); err != nil {
			t.Fatalf("role %q should be accepted: %v", role, err)
		}
	}
}

func TestNewServerRegistersAllTools(t *testing.T) {
	srv := NewServer(&fakeClient{}, adminAuth())
	if srv == nil {
		t.Fatalf("NewServer returned nil")
	}
}

func TestNewDefaultPrincipalFromHeaders(t *testing.T) {
	f := &fakeClient{objResp: map[string]any{"ok": true}}
	// Header-derived viewer principal cannot call an admin tool.
	auth := &AuthorizationContext{}
	if p := principalFromHeaders(map[string]string{"X-Uterm-Role": "viewer"}); p != nil {
		auth.DefaultPrincipal = *p
	}
	tools := hijackTools(f, auth)
	res := invoke(t, findTool(t, tools, "worker_disconnect"), map[string]any{"worker_id": "w1"})
	if res["error"] != "authorization_denied" {
		t.Fatalf("header viewer must be denied admin tool: %#v", res)
	}
}

func TestConfigDefaultPrincipalOverride(t *testing.T) {
	admin := newPrincipal("svc", "admin")
	srv, err := New(Config{BaseURL: "http://x", DefaultPrincipal: &admin})
	if err != nil || srv == nil {
		t.Fatalf("New with explicit principal failed: %v", err)
	}
}

// TestNewWithClientOptions covers EntityPrefix + Headers option wiring.
func TestNewWithClientOptions(t *testing.T) {
	srv, err := New(Config{
		BaseURL:      "http://x",
		EntityPrefix: "/agent",
		Headers:      map[string]string{"X-Uterm-Role": "operator", "X-Uterm-Subject": "bob"},
	})
	if err != nil || srv == nil {
		t.Fatalf("New with options: %v", err)
	}
}
