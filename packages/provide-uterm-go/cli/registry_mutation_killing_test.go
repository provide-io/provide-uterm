//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Mutation-killing suite for cli/registry.go, which joined the gremlins
// perimeter (ci/mutation_gate.py) so the Go session registry is held to the
// same bar as the Python SessionRegistry. These tests exist to kill specific
// mutants rather than to add coverage: each one names the mutation it defeats.

package cli

import (
	"context"
	"testing"
)

// sessionIDValid's charset switch is one line of eight comparisons, each of
// which gremlins mutates in both directions (boundary and negation). Every
// accepted class needs its two edge characters accepted, and the character
// immediately outside each range rejected — a table that only tried "abc" and
// "!" would leave every boundary mutant alive.
func TestSessionIDValidCharsetBoundaries(t *testing.T) {
	valid := []string{
		"a", "z", // c >= 'a' && c <= 'z'
		"A", "Z", // c >= 'A' && c <= 'Z'
		"0", "9", // c >= '0' && c <= '9'
		"_", "-", // c == '_', c == '-'
		"aZ0_-", // all classes in one id
	}
	for _, id := range valid {
		if !sessionIDValid(id) {
			t.Errorf("sessionIDValid(%q) = false, want true", id)
		}
	}

	// One character below/above each accepted range, so widening or narrowing
	// any bound flips a result.
	invalid := []string{
		"`", "{", // 'a'-1, 'z'+1
		"@", "[", // 'A'-1, 'Z'+1
		"/", ":", // '0'-1, '9'+1
		"^", ",", // '_'-1, '-'-1
		"`a", "a{", // a bad character anywhere fails the whole id
		"", // the empty guard, ahead of the loop
	}
	for _, id := range invalid {
		if sessionIDValid(id) {
			t.Errorf("sessionIDValid(%q) = true, want false", id)
		}
	}
}

// DeleteSession removes the id from r.order by scanning for it. Negating the
// `oid == id` comparison would drop the first NON-matching entry instead, which
// only a delete from the middle of a multi-session order can catch: with one
// session, or when deleting the head, both versions agree.
func TestDeleteSessionRemovesOnlyTheNamedIDFromOrder(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()
	for _, id := range []string{"alpha", "beta", "gamma"} {
		if _, err := r.CreateSession(ctx, map[string]any{"session_id": id, "connector_type": "shell"}); err != nil {
			t.Fatalf("create %s: %v", id, err)
		}
	}
	// newTestRegistry seeds provide-shell, so assert on the tail we control.
	if err := r.DeleteSession(ctx, "beta"); err != nil {
		t.Fatalf("delete: %v", err)
	}

	r.mu.Lock()
	order := append([]string(nil), r.order...)
	_, betaPresent := r.entries["beta"]
	r.mu.Unlock()

	if betaPresent {
		t.Fatal("beta survived DeleteSession")
	}
	var seen []string
	for _, id := range order {
		if id == "alpha" || id == "beta" || id == "gamma" {
			seen = append(seen, id)
		}
	}
	if len(seen) != 2 || seen[0] != "alpha" || seen[1] != "gamma" {
		t.Fatalf("order = %v, want alpha then gamma with beta removed", seen)
	}
}

// definitionFromPayload defaults a missing connector_config to an empty map.
// Negating `cc == nil` would both leave a missing config nil AND discard a
// supplied one, so each half needs its own assertion.
func TestCreateSessionConnectorConfigDefaultAndPassthrough(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()

	if _, err := r.CreateSession(ctx, map[string]any{"session_id": "no-cc", "connector_type": "shell"}); err != nil {
		t.Fatalf("create: %v", err)
	}
	def, ok := r.GetDefinition(ctx, "no-cc")
	if !ok {
		t.Fatal("no-cc missing")
	}
	if def.ConnectorConfig == nil {
		t.Fatal("a missing connector_config must default to an empty map, not nil")
	}
	if len(def.ConnectorConfig) != 0 {
		t.Fatalf("connector_config = %v, want empty", def.ConnectorConfig)
	}

	payload := map[string]any{
		"session_id":       "with-cc",
		"connector_type":   "shell",
		"connector_config": map[string]any{"command": "/bin/zsh"},
	}
	if _, err := r.CreateSession(ctx, payload); err != nil {
		t.Fatalf("create with cc: %v", err)
	}
	def, ok = r.GetDefinition(ctx, "with-cc")
	if !ok {
		t.Fatal("with-cc missing")
	}
	if def.ConnectorConfig["command"] != "/bin/zsh" {
		t.Fatalf("connector_config = %v, want the supplied command preserved", def.ConnectorConfig)
	}
}
