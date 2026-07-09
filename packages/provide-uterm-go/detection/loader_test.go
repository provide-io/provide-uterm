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

func writeRules(t *testing.T, body string) RulesPath {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "rules.json")
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return RulesPath(path)
}

func TestLoadRulesetFromPath(t *testing.T) {
	p := writeRules(t, `{"game":"test","prompts":[{"id":"prompt.hello","match":{"pattern":"Hello there","match_mode":"contains"},"input_type":"single_key"}]}`)
	rs, err := LoadRuleset(p)
	if err != nil {
		t.Fatal(err)
	}
	if rs.Game != "test" || len(rs.Prompts) != 1 {
		t.Error("loaded content")
	}
}

func TestLoadRulesetMissingPath(t *testing.T) {
	_, err := LoadRuleset(RulesPath("/no/such/file.json"))
	if err == nil || !strings.Contains(err.Error(), "Rules file not found") {
		t.Errorf("err = %v", err)
	}
}

func TestLoadRulesetFromString(t *testing.T) {
	rs, err := LoadRuleset(`{"version":"1.0","game":"testgame","prompts":[{"id":"prompt.hello","match":{"pattern":"Hello","match_mode":"contains"}}]}`)
	if err != nil {
		t.Fatal(err)
	}
	if rs.Game != "testgame" || rs.Prompts[0].ID != "prompt.hello" {
		t.Error("content")
	}
}

func TestLoadRulesetBadString(t *testing.T) {
	if _, err := LoadRuleset("{not valid json"); err == nil || !strings.Contains(err.Error(), "Failed to parse rules") {
		t.Errorf("err = %v", err)
	}
	if _, err := LoadRuleset(`{"version":"1.0","prompts":[]}`); err == nil || !strings.Contains(err.Error(), "Failed to parse rules") {
		t.Errorf("missing game err = %v", err)
	}
}

func TestLoadRulesetPassthrough(t *testing.T) {
	orig := &RuleSet{Game: "passthrough", Version: "2.0"}
	result, err := LoadRuleset(orig)
	if err != nil {
		t.Fatal(err)
	}
	if result != orig {
		t.Error("should return same pointer")
	}
	// value form
	valResult, err := LoadRuleset(RuleSet{Game: "byvalue"})
	if err != nil {
		t.Fatal(err)
	}
	if valResult.Game != "byvalue" {
		t.Error("value passthrough")
	}
}

func TestLoadRulesetUnsupportedType(t *testing.T) {
	if _, err := LoadRuleset(42); err == nil {
		t.Error("expected error for unsupported type")
	}
}

func TestRuleSetFromJSONInvalid(t *testing.T) {
	if _, err := RuleSetFromJSON([]byte("not json")); err == nil {
		t.Error("expected error")
	}
}
