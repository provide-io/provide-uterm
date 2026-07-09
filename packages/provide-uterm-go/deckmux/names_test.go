//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import "testing"

// TestGoldenNames validates GenerateName/GenerateColor/GenerateInitials against
// the Python outputs — this proves the 256-bit big-int hash port is exact.
func TestGoldenNames(t *testing.T) {
	var names []struct {
		ID       string `json:"id"`
		Name     string `json:"name"`
		Color    string `json:"color"`
		Initials string `json:"initials"`
	}
	goldenCase(t, "names", &names)
	if len(names) == 0 {
		t.Fatal("no golden names")
	}
	for _, c := range names {
		if got := GenerateName(c.ID); got != c.Name {
			t.Errorf("GenerateName(%q) = %q, want %q", c.ID, got, c.Name)
		}
		if got := GenerateColor(c.ID, nil); got != c.Color {
			t.Errorf("GenerateColor(%q) = %q, want %q", c.ID, got, c.Color)
		}
		if got := GenerateInitials(c.Name); got != c.Initials {
			t.Errorf("GenerateInitials(%q) = %q, want %q", c.Name, got, c.Initials)
		}
	}
}

func TestGoldenColorTaken(t *testing.T) {
	var ct struct {
		ID    string   `json:"id"`
		Taken []string `json:"taken"`
		Color string   `json:"color"`
	}
	goldenCase(t, "color_taken", &ct)
	taken := make(map[string]struct{})
	for _, c := range ct.Taken {
		taken[c] = struct{}{}
	}
	if got := GenerateColor(ct.ID, taken); got != ct.Color {
		t.Errorf("GenerateColor(%q, taken) = %q, want %q", ct.ID, got, ct.Color)
	}
}

func TestGoldenInitials(t *testing.T) {
	var cases []struct {
		Name     string `json:"name"`
		Initials string `json:"initials"`
	}
	goldenCase(t, "initials", &cases)
	for _, c := range cases {
		if got := GenerateInitials(c.Name); got != c.Initials {
			t.Errorf("GenerateInitials(%q) = %q, want %q", c.Name, got, c.Initials)
		}
	}
}

func TestHashIntDeterministic(t *testing.T) {
	if hashInt("test").Cmp(hashInt("test")) != 0 {
		t.Error("hashInt not deterministic")
	}
	if hashInt("a").Cmp(hashInt("b")) == 0 {
		t.Error("hashInt collision for a/b")
	}
}

func TestGenerateColorDeterministicAndValid(t *testing.T) {
	if a, b := GenerateColor("conn-1", nil), GenerateColor("conn-1", nil); a != b {
		t.Error("not deterministic")
	}
	c := GenerateColor("test", nil)
	if len(c) != 7 || c[0] != '#' {
		t.Errorf("invalid hex %q", c)
	}
}

func TestGenerateColorAvoidsTaken(t *testing.T) {
	def := GenerateColor("test-id", nil)
	got := GenerateColor("test-id", map[string]struct{}{def: {}})
	if got == def {
		t.Error("did not avoid taken color")
	}
}

func TestGenerateColorAllTakenFallback(t *testing.T) {
	allTaken := make(map[string]struct{}, len(colors))
	for _, c := range colors {
		allTaken[c] = struct{}{}
	}
	got := GenerateColor("test-id", allTaken)
	// Falls back to the hash-based default (index base).
	want := colors[modInt(hashInt("test-id"), len(colors))]
	if got != want {
		t.Errorf("fallback = %q, want %q", got, want)
	}
}

func TestGenerateColorForwardNotBackward(t *testing.T) {
	h := hashInt("forward-test")
	base := modInt(h, len(colors))
	natural := colors[base]
	forwardNext := colors[(base+1)%len(colors)]
	got := GenerateColor("forward-test", map[string]struct{}{natural: {}})
	if got != forwardNext {
		t.Errorf("avoidance = %q, want forward %q", got, forwardNext)
	}
}

func TestGenerateNameFormat(t *testing.T) {
	name := GenerateName("some-connection")
	parts := []rune(name)
	_ = parts
	adj, animal := splitTwo(t, name)
	if !contains(adjectives, lower(adj)) {
		t.Errorf("adjective %q not in table", adj)
	}
	if !contains(animals, lower(animal)) {
		t.Errorf("animal %q not in table", animal)
	}
}

func TestGenerateInitialsEdgeCases(t *testing.T) {
	cases := map[string]string{
		"Red Fox": "RF", "red fox": "RF", "Alice": "AL", "A B C": "AB", "A": "A",
	}
	for name, want := range cases {
		if got := GenerateInitials(name); got != want {
			t.Errorf("GenerateInitials(%q) = %q, want %q", name, got, want)
		}
	}
}

// TestNameHelpers directly exercises the small helpers' edge branches.
func TestNameHelpers(t *testing.T) {
	if titleWord("") != "" {
		t.Error("titleWord empty")
	}
	if titleWord("red") != "Red" {
		t.Error("titleWord")
	}
	if firstRune("") != "" {
		t.Error("firstRune empty")
	}
	if firstRune("abc") != "a" {
		t.Error("firstRune")
	}
	if firstRunes("héllo", 2) != "hé" {
		t.Errorf("firstRunes = %q", firstRunes("héllo", 2))
	}
	if firstRunes("a", 2) != "a" {
		t.Error("firstRunes short")
	}
}

// --- tiny local helpers for the name tests ---

func splitTwo(t *testing.T, s string) (string, string) {
	t.Helper()
	for i, r := range s {
		if r == ' ' {
			return s[:i], s[i+1:]
		}
	}
	t.Fatalf("name %q not two words", s)
	return "", ""
}

func lower(s string) string {
	b := []rune(s)
	for i, r := range b {
		if r >= 'A' && r <= 'Z' {
			b[i] = r + 32
		}
	}
	return string(b)
}

func contains(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}
