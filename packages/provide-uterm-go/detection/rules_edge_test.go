//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"encoding/json"
	"testing"
)

// Malformed-JSON error branches for every custom UnmarshalJSON.
func TestUnmarshalErrorBranches(t *testing.T) {
	bad := []byte(`{"pattern": 42}`) // pattern must be a string
	if err := new(RegexRule).UnmarshalJSON(bad); err == nil {
		t.Error("RegexRule")
	}
	if err := new(ScreenConstraint).UnmarshalJSON([]byte(`{"expect_cursor_at_end": "x"}`)); err == nil {
		t.Error("ScreenConstraint")
	}
	if err := new(KVExtractRule).UnmarshalJSON([]byte(`{"field": 42}`)); err == nil {
		t.Error("KVExtractRule")
	}
	if err := new(PromptRule).UnmarshalJSON([]byte(`{"id": 42}`)); err == nil {
		t.Error("PromptRule")
	}
	if err := new(TimingRule).UnmarshalJSON([]byte(`{"min_wait_ms": "x"}`)); err == nil {
		t.Error("TimingRule")
	}
	if err := new(ActionRule).UnmarshalJSON([]byte(`{"id": 42}`)); err == nil {
		t.Error("ActionRule")
	}
	if err := new(RuleSet).UnmarshalJSON([]byte(`{"game": 42}`)); err == nil {
		t.Error("RuleSet")
	}
}

func TestScreenConstraintCursorBounds(t *testing.T) {
	var sc ScreenConstraint
	if err := json.Unmarshal([]byte(`{"expect_cursor_at_end": false, "cursor_row_min": 1, "cursor_row_max": 2, "cursor_col_min": 3, "cursor_col_max": 4}`), &sc); err != nil {
		t.Fatal(err)
	}
	if sc.ExpectCursorAtEnd || *sc.CursorRowMin != 1 || *sc.CursorRowMax != 2 || *sc.CursorColMin != 3 || *sc.CursorColMax != 4 {
		t.Errorf("sc = %+v", sc)
	}
}

func TestKVExtractRuleEmptyTypeDefaults(t *testing.T) {
	var k KVExtractRule
	if err := json.Unmarshal([]byte(`{"field":"f","regex":"r","type":"","flags":8,"required":true}`), &k); err != nil {
		t.Fatal(err)
	}
	if k.Type != "string" || k.Flags != 8 || !k.Required {
		t.Errorf("k = %+v", k)
	}
}

func TestPromptRuleExplicitKindAndInputType(t *testing.T) {
	var p PromptRule
	if err := json.Unmarshal([]byte(`{"id":"p","kind":"menu","input_type":"single_key","match":{"pattern":"x","match_mode":"regex"}}`), &p); err != nil {
		t.Fatal(err)
	}
	if p.Kind != "menu" || p.InputType != "single_key" {
		t.Errorf("p = %+v", p)
	}
	// explicit empty strings fall back to the defaults
	var p2 PromptRule
	if err := json.Unmarshal([]byte(`{"id":"p","kind":"","input_type":"","match":{"pattern":"x","match_mode":"regex"}}`), &p2); err != nil {
		t.Fatal(err)
	}
	if p2.Kind != "unknown" || p2.InputType != "multi_key" {
		t.Errorf("p2 = %+v", p2)
	}
}

func TestToPromptPatternsIncludesNotes(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{"game":"t","prompts":[
		{"id":"p","match":{"pattern":"x","match_mode":"regex"},"notes":"a helpful note"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if rs.ToPromptPatterns()[0]["notes"] != "a helpful note" {
		t.Error("notes carried through")
	}
}

func TestRuleSetExplicitVersionAndMetadata(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{"game":"g","version":"2.0","metadata":{"k":"v"},
		"menus":[{"id":"m","prompt_match":{"pattern":"x","match_mode":"regex"},"options":[{"key":"1","label":"One"}]}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if rs.Version != "2.0" || rs.Metadata["k"] != "v" {
		t.Errorf("rs = %+v", rs)
	}
	if len(rs.Menus) != 1 || rs.Menus[0].Options[0].Label != "One" {
		t.Errorf("menus = %+v", rs.Menus)
	}
}

func TestPatternIDNonString(t *testing.T) {
	if patternID(Pattern{"id": 42}) != "unknown" {
		t.Error("non-string id")
	}
	if patternID(Pattern{}) != "unknown" {
		t.Error("missing id")
	}
	if patternID(Pattern{"id": "x"}) != "x" {
		t.Error("string id")
	}
}
