//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package policy

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestCanInject(t *testing.T) {
	p := &Strict{}
	if err := p.CanInject("session123", "lease456", "bob", "viewer"); err == nil {
		t.Fatal("expected error for viewer role, got nil")
	}
	if err := p.CanInject("session123", "lease456", "bob", "operator"); err != nil {
		t.Fatalf("operator with lease: %v", err)
	}
	if err := p.CanInject("session123", "", "bob", "operator"); err == nil {
		t.Fatal("expected error for empty lease")
	}
	if err := p.CanInject("session123", "lease456", "bob", "admin"); err != nil {
		t.Fatalf("admin with lease: %v", err)
	}
}

func TestUnknownOp(t *testing.T) {
	p := &Strict{}
	err := p.CanPerform("nope", "admin", true, true)
	if err == nil || err.Error() != "forbidden: unknown operation nope" {
		t.Fatalf("got %v", err)
	}
}

func TestSessionInactive(t *testing.T) {
	p := &Strict{}
	err := p.CanPerform("hijack_step", "operator", true, false)
	if err == nil || err.Error() != ErrSessionInactive {
		t.Fatalf("got %v", err)
	}
}

type behaviorVectors struct {
	PolicyCases []struct {
		Op            string  `json:"op"`
		Role          string  `json:"role"`
		LeaseOwned    bool    `json:"lease_owned"`
		SessionActive bool    `json:"session_active"`
		Allowed       bool    `json:"allowed"`
		Error         *string `json:"error"`
	} `json:"policy_cases"`
	HelloDefaults map[string]map[string]bool `json:"hello_defaults"`
}

func loadVectors(t *testing.T) behaviorVectors {
	t.Helper()
	// Prefer package testdata; fall back to repo-root spec via relative walk.
	candidates := []string{
		filepath.Join("testdata", "behavior_vectors.json"),
		filepath.Join("..", "..", "..", "spec", "behavior_vectors.json"),
	}
	var raw []byte
	var err error
	for _, path := range candidates {
		raw, err = os.ReadFile(path) //nolint:gosec
		if err == nil {
			break
		}
	}
	if err != nil {
		t.Fatalf("read vectors: %v", err)
	}
	var v behaviorVectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatalf("parse vectors: %v", err)
	}
	return v
}

func TestPolicyMatchesBehaviorVectors(t *testing.T) {
	v := loadVectors(t)
	p := &Strict{}
	for i, c := range v.PolicyCases {
		err := p.CanPerform(c.Op, c.Role, c.LeaseOwned, c.SessionActive)
		if c.Allowed {
			if err != nil {
				t.Errorf("case %d %+v: want allow, got %v", i, c, err)
			}
			continue
		}
		if err == nil {
			t.Errorf("case %d %+v: want deny, got allow", i, c)
			continue
		}
		want := ""
		if c.Error != nil {
			want = *c.Error
		}
		if err.Error() != want {
			t.Errorf("case %d %+v: got %q want %q", i, c, err.Error(), want)
		}
	}
}

func TestHelloDefaultsDocumented(t *testing.T) {
	v := loadVectors(t)
	goDefs := v.HelloDefaults["go"]
	if !goDefs["mcp_supported"] || !goDefs["vnc_supported"] {
		t.Fatalf("unexpected go defaults: %#v", goDefs)
	}
}
