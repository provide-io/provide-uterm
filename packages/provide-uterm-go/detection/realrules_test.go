//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"io/fs"
	"path/filepath"
	"strings"
	"testing"
)

// TestRealRulesFilesCompileUnderRE2 walks the repository for real rules.json
// files (the games/{namespace}/rules.json convention) and asserts every
// positive, negative, and kv_extract pattern compiles under RE2 after the
// trivial translations. As of 2026-07-09 the repo contains no rules.json
// files (verified via `git ls-files`/`find` including history), so the walk
// finding nothing is expected and the test records that instead of failing.
func TestRealRulesFilesCompileUnderRE2(t *testing.T) {
	repoRoot := "../../.." // packages/provide-uterm-go/detection -> repo root
	var rulesFiles []string
	err := filepath.WalkDir(repoRoot, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil //nolint:nilerr // unreadable dirs are skipped, not fatal
		}
		if d.IsDir() {
			name := d.Name()
			if name == "node_modules" || name == ".git" || name == ".venv" || name == "__pycache__" {
				return fs.SkipDir
			}
			return nil
		}
		if d.Name() == "rules.json" {
			rulesFiles = append(rulesFiles, path)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walk: %v", err)
	}
	if len(rulesFiles) == 0 {
		t.Log("no rules.json files found in the repository; nothing to verify (expected as of 2026-07)")
		return
	}
	for _, path := range rulesFiles {
		rs, err := RuleSetFromJSONFile(path)
		if err != nil {
			t.Errorf("%s: load failed: %v", path, err)
			continue
		}
		bad := CheckRuleSetRE2(rs)
		if len(bad) > 0 {
			t.Errorf("%s: %d pattern(s) not RE2-compatible:\n  %s",
				path, len(bad), strings.Join(bad, "\n  "))
		}
	}
}

func TestCheckRuleSetRE2(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{
		"game": "t",
		"prompts": [
			{"id": "ok", "match": {"pattern": "Command \\[.*\\Z", "match_mode": "regex"},
			 "negative_match": {"pattern": "stardock", "match_mode": "contains"},
			 "kv_extract": [{"field": "n", "regex": "N:\\s*(\\d+)", "type": "int"}]},
			{"id": "contains_ok", "match": {"pattern": "[Press ENTER]", "match_mode": "contains"}}
		]}`))
	if err != nil {
		t.Fatal(err)
	}
	if bad := CheckRuleSetRE2(rs); len(bad) != 0 {
		t.Errorf("expected clean, got %v", bad)
	}

	badRS, err := RuleSetFromJSON([]byte(`{
		"game": "t",
		"prompts": [
			{"id": "look", "match": {"pattern": "foo(?=bar)", "match_mode": "regex"},
			 "negative_match": {"pattern": "(a)\\1", "match_mode": "regex"},
			 "kv_extract": [{"field": "x", "regex": "(?<=y)z", "type": "string"}]}
		]}`))
	if err != nil {
		t.Fatal(err)
	}
	bad := CheckRuleSetRE2(badRS)
	if len(bad) != 3 {
		t.Fatalf("expected 3 offenders, got %d: %v", len(bad), bad)
	}
	for _, b := range bad {
		if !strings.Contains(b, "look") {
			t.Errorf("offender should name the prompt: %q", b)
		}
	}
}
