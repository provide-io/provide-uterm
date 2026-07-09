//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRuleSetDefaults(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{"game":"mygame"}`))
	if err != nil {
		t.Fatal(err)
	}
	if rs.Version != "1.0" {
		t.Errorf("version = %q", rs.Version)
	}
	if len(rs.Prompts) != 0 || len(rs.Menus) != 0 || len(rs.Flows) != 0 || len(rs.Metadata) != 0 {
		t.Error("defaults not empty")
	}
	if rs.Game != "mygame" {
		t.Error("game")
	}
}

func TestRuleSetMissingGameErrors(t *testing.T) {
	if _, err := RuleSetFromJSON([]byte(`{"version":"1.0","prompts":[]}`)); err == nil {
		t.Fatal("expected error for missing game")
	}
}

func TestRegexRuleToRegex(t *testing.T) {
	regexMode := RegexRule{Pattern: `Sector\s+\d+`, MatchMode: "regex"}
	if got, ok := regexMode.ToRegex(); !ok || got != `Sector\s+\d+` {
		t.Errorf("regex mode got %q ok=%v", got, ok)
	}
	contains := RegexRule{Pattern: "Enter your name", MatchMode: "contains"}
	if got, ok := contains.ToRegex(); !ok || got != `Enter\ your\ name` {
		t.Errorf("contains got %q", got)
	}
	exact := RegexRule{Pattern: "[Press ENTER]", MatchMode: "exact"}
	if got, ok := exact.ToRegex(); !ok || got != `^\[Press\ ENTER\]$` {
		t.Errorf("exact got %q", got)
	}
	unknown := RegexRule{Pattern: "x", MatchMode: "bogus"}
	if got, ok := unknown.ToRegex(); ok || got != "" {
		t.Errorf("unknown should be none: got %q ok=%v", got, ok)
	}
}

func TestRegexRuleDefaults(t *testing.T) {
	var r RegexRule
	if err := r.UnmarshalJSON([]byte(`{"pattern":"x"}`)); err != nil {
		t.Fatal(err)
	}
	if r.MatchMode != "regex" {
		t.Errorf("default match_mode = %q", r.MatchMode)
	}
	if r.Flags != reMultilineIgnorecase {
		t.Errorf("default flags = %d", r.Flags)
	}
	// explicit flags respected
	if err := r.UnmarshalJSON([]byte(`{"pattern":"x","flags":2,"match_mode":""}`)); err != nil {
		t.Fatal(err)
	}
	if r.Flags != 2 || r.MatchMode != "regex" {
		t.Errorf("flags=%d match_mode=%q", r.Flags, r.MatchMode)
	}
}

func TestToPromptPatternsRoundTrip(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{
		"game":"test",
		"prompts":[
			{"id":"prompt.login","match":{"pattern":"Enter your name","match_mode":"contains"},"input_type":"multi_key"},
			{"id":"prompt.sector","match":{"pattern":"Sector\\s+\\d+","match_mode":"regex"},
			 "kv_extract":[{"field":"credits","regex":"Credits:\\s*(\\d+)","type":"int"}]},
			{"id":"prompt.menu","match":{"pattern":"choose","match_mode":"contains"},
			 "negative_match":{"pattern":"do not show","match_mode":"contains"}}
		]}`))
	if err != nil {
		t.Fatal(err)
	}
	patterns := rs.ToPromptPatterns()
	if len(patterns) != 3 {
		t.Fatalf("count = %d", len(patterns))
	}
	p0 := patterns[0]
	if p0["id"] != "prompt.login" || p0["input_type"] != "multi_key" {
		t.Error("p0 basics")
	}
	if p0["regex"] != `Enter\ your\ name` {
		t.Errorf("p0 regex = %v", p0["regex"])
	}
	if p0["auto_detected"] != false {
		t.Error("auto_detected")
	}
	if _, has := p0["negative_regex"]; has {
		t.Error("p0 should have no negative_regex")
	}
	if _, has := p0["kv_extract"]; has {
		t.Error("p0 should have no kv_extract")
	}
	if patterns[1]["regex"] != `Sector\s+\d+` {
		t.Errorf("p1 regex = %v", patterns[1]["regex"])
	}
	kv, ok := patterns[1]["kv_extract"].([]any)
	if !ok || len(kv) != 1 {
		t.Fatal("kv_extract shape")
	}
	kv0 := kv[0].(map[string]any)
	if kv0["field"] != "credits" || kv0["regex"] != `Credits:\s*(\d+)` || kv0["type"] != "int" {
		t.Error("kv fields")
	}
	if patterns[2]["negative_regex"] != `do\ not\ show` {
		t.Errorf("negative_regex = %v", patterns[2]["negative_regex"])
	}
}

func TestToPromptPatternsEmpty(t *testing.T) {
	rs := &RuleSet{Game: "test"}
	if len(rs.ToPromptPatterns()) != 0 {
		t.Error("empty prompts")
	}
}

func TestToPromptPatternsOrderPreserved(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{"game":"t","prompts":[
		{"id":"prompt.a","match":{"pattern":"AAA","match_mode":"contains"}},
		{"id":"prompt.b","match":{"pattern":"BBB","match_mode":"contains"}},
		{"id":"prompt.c","match":{"pattern":"CCC","match_mode":"contains"}}]}`))
	if err != nil {
		t.Fatal(err)
	}
	ids := []string{}
	for _, p := range rs.ToPromptPatterns() {
		ids = append(ids, p["id"].(string))
	}
	if strings.Join(ids, ",") != "prompt.a,prompt.b,prompt.c" {
		t.Errorf("order = %v", ids)
	}
}

func TestPromptRuleDefaults(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{"game":"t","prompts":[{"id":"p","match":{"pattern":"x","match_mode":"regex"}}]}`))
	if err != nil {
		t.Fatal(err)
	}
	p := rs.Prompts[0]
	if p.Kind != "unknown" {
		t.Errorf("kind = %q", p.Kind)
	}
	if p.InputType != "multi_key" {
		t.Errorf("input_type = %q", p.InputType)
	}
	if !p.Screen.ExpectCursorAtEnd {
		t.Error("expect_cursor_at_end default true")
	}
}

func TestPromptRuleExplicitScreenConstraint(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{"game":"t","prompts":[{"id":"p","match":{"pattern":"x","match_mode":"regex"},"screen":{"expect_cursor_at_end":false}}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if rs.Prompts[0].Screen.ExpectCursorAtEnd {
		t.Error("explicit false not honored")
	}
	if rs.ToPromptPatterns()[0]["expect_cursor_at_end"] != false {
		t.Error("expect_cursor_at_end in pattern")
	}
}

func TestKVExtractRuleDefaultsAndAlias(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{"game":"t","prompts":[{"id":"p","match":{"pattern":"x","match_mode":"regex"},
		"kv_extract":[{"field":"f","regex":"(\\d+)","validate":{"min":0}}]}]}`))
	if err != nil {
		t.Fatal(err)
	}
	k := rs.Prompts[0].KVExtract[0]
	if k.Type != "string" {
		t.Errorf("default type = %q", k.Type)
	}
	if k.Flags != reMultilineIgnorecase {
		t.Errorf("default flags = %d", k.Flags)
	}
	if k.Validate["min"] != float64(0) {
		t.Errorf("validate alias = %v", k.Validate)
	}
}

func TestTimingAndActionDefaults(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{"game":"t","flows":[{"id":"f","description":"d","steps":[
		{"id":"s","kind":"send_keys"}]}]}`))
	if err != nil {
		t.Fatal(err)
	}
	step := rs.Flows[0].Steps[0]
	if step.Timing.MaxWaitMs != 8000 || step.Timing.RetryMs != 250 || !step.Timing.RequireStableScreen {
		t.Errorf("timing defaults = %+v", step.Timing)
	}
	if _, ok := step.KeysOrNil(); ok {
		t.Error("keys should be nil")
	}
	// explicit timing
	rs2, err := RuleSetFromJSON([]byte(`{"game":"t","flows":[{"id":"f","description":"d","steps":[
		{"id":"s","kind":"send_keys","keys":"x","timing":{"max_wait_ms":10,"min_wait_ms":1,"retry_ms":2,"require_stable_screen":false}}]}]}`))
	if err != nil {
		t.Fatal(err)
	}
	s2 := rs2.Flows[0].Steps[0]
	if s2.Timing.MaxWaitMs != 10 || s2.Timing.MinWaitMs != 1 || s2.Timing.RetryMs != 2 || s2.Timing.RequireStableScreen {
		t.Errorf("explicit timing = %+v", s2.Timing)
	}
	if k, ok := s2.KeysOrNil(); !ok || k != "x" {
		t.Error("keys")
	}
}

func TestRuleSetFromJSONFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "rules.json")
	if err := os.WriteFile(path, []byte(`{"game":"filegame","prompts":[{"id":"p","match":{"pattern":"x","match_mode":"regex"}}]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	rs, err := RuleSetFromJSONFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if rs.Game != "filegame" {
		t.Error("game")
	}
	// bad json
	bad := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(bad, []byte("{not valid json"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := RuleSetFromJSONFile(bad); err == nil || !strings.Contains(err.Error(), "Failed to load rules") {
		t.Errorf("bad json err = %v", err)
	}
	// missing file
	if _, err := RuleSetFromJSONFile(filepath.Join(dir, "nope.json")); err == nil {
		t.Error("missing file should error")
	}
	// missing required field
	missing := filepath.Join(dir, "missing.json")
	if err := os.WriteFile(missing, []byte(`{"version":"1.0","prompts":[]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := RuleSetFromJSONFile(missing); err == nil || !strings.Contains(err.Error(), "Failed to load rules") {
		t.Errorf("missing field err = %v", err)
	}
}
