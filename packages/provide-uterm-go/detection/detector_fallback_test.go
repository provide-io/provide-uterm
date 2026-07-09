//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"strings"
	"testing"
)

// --- cursor miss / fallback --------------------------------------------------

func tallScreen() string {
	filler := strings.Repeat("line\n", 24) + "line"
	return "Enter your name:\n" + filler
}

func cursorPatterns() []Pattern {
	return []Pattern{{
		"id": "prompt.login", "regex": `Enter your name:`, "input_type": "multi_key",
		"eol_pattern": "$", "expect_cursor_at_end": true,
	}}
}

func TestCursorMissWithoutTrailingSpace(t *testing.T) {
	d := mustDetector(cursorPatterns())
	snapshot := Snapshot{
		"screen": tallScreen(), "screen_hash": "abc",
		"cursor_at_end": false, "has_trailing_space": false,
		"cursor": map[string]any{"y": 0, "x": 0},
	}
	diag := d.DetectPromptWithDiagnostics(snapshot)
	if diag.Match != nil {
		t.Error("no fallback without trailing space")
	}
	found := false
	for _, r := range diag.RegexMatchedButFailed {
		if r["reason"] == "cursor_position" {
			found = true
		}
	}
	if !found {
		t.Error("cursor_position failure recorded")
	}
}

func TestCursorMissFallbackWithTrailingSpace(t *testing.T) {
	d := mustDetector(cursorPatterns())
	snapshot := Snapshot{
		"screen": tallScreen(), "screen_hash": "abc",
		"cursor_at_end": false, "has_trailing_space": true,
		"cursor": map[string]any{"y": 0, "x": 0},
	}
	result := d.DetectPrompt(snapshot)
	if result == nil || result.PromptID != "prompt.login" {
		t.Fatalf("fallback = %+v", result)
	}
}

func TestFullScreenFallbackWhenCursorNotInRegion(t *testing.T) {
	d := mustDetector(cursorPatterns())
	snapshot := Snapshot{
		"screen": tallScreen(), "screen_hash": "x",
		"cursor_at_end": true, "has_trailing_space": false,
		"cursor": map[string]any{"y": 0, "x": 0},
	}
	result := d.DetectPrompt(snapshot)
	if result == nil || result.PromptID != "prompt.login" {
		t.Fatalf("full-screen fallback = %+v", result)
	}
}

func TestDiagnosticsEmptyScreen(t *testing.T) {
	d := mustDetector(makePatterns())
	diag := d.DetectPromptWithDiagnostics(Snapshot{"screen": "", "cursor_at_end": true})
	if diag.Match != nil {
		t.Error("empty screen no match")
	}
}

func TestCursorMissCandidateIncludesKVExtract(t *testing.T) {
	kvCfg := []any{map[string]any{"field": "score", "regex": `Score:\s+(\d+)`, "type": "int"}}
	filler := strings.Repeat("line\n", 24) + "line"
	patterns := []Pattern{{
		"id": "prompt.score", "regex": `Score:`, "input_type": "multi_key",
		"expect_cursor_at_end": true, "kv_extract": kvCfg,
	}}
	d := mustDetector(patterns)
	snapshot := Snapshot{
		"screen": "Score: 100\n" + filler, "screen_hash": "abc",
		"cursor_at_end": false, "has_trailing_space": true,
		"cursor": map[string]any{"y": 0, "x": 0},
	}
	result := d.DetectPrompt(snapshot)
	if result == nil {
		t.Fatal("no fallback match")
	}
	kv, ok := result.KVExtract.([]any)
	if !ok || len(kv) != 1 {
		t.Errorf("kv_extract = %#v", result.KVExtract)
	}
}

func TestNegativeFailureRecordedInDiagnostics(t *testing.T) {
	patterns := []Pattern{{
		"id": "p", "regex": `which item`, "input_type": "single_key",
		"negative_regex": "stardock",
	}}
	d := mustDetector(patterns)
	diag := d.DetectPromptWithDiagnostics(snap("stardock\nwhich item"))
	if diag.Match != nil {
		t.Fatal("should be excluded")
	}
	if len(diag.RegexMatchedButFailed) == 0 || diag.RegexMatchedButFailed[0]["reason"] != "negative_match" {
		t.Errorf("failures = %v", diag.RegexMatchedButFailed)
	}
	if diag.RegexMatchedButFailed[0]["negative_pattern"] != "stardock" {
		t.Error("negative_pattern recorded")
	}
}

func TestNoCursorEndSubsetUsedWhenCursorNotAtEnd(t *testing.T) {
	// A pattern with expect_cursor_at_end=false must match at the region pass
	// even when cursor_at_end is false.
	patterns := []Pattern{{
		"id": "p.more", "regex": `more`, "input_type": "any_key", "expect_cursor_at_end": false,
	}}
	d := mustDetector(patterns)
	s := snapFor("press any key for more", false, 0)
	if got := d.DetectPrompt(s); got == nil || got.PromptID != "p.more" {
		t.Fatalf("got %+v", got)
	}
}

func TestNegativeRegexInvalidIsIgnored(t *testing.T) {
	// An invalid negative regex cannot be precompiled; the pattern still
	// matches positively (Python would raise at detect time; the Go port
	// treats an uncompilable negative as absent — see deviation notes).
	patterns := []Pattern{{
		"id": "p", "regex": `which item`, "input_type": "single_key",
		"negative_regex": `[bad`,
	}}
	d := mustDetector(patterns)
	if d.DetectPrompt(snap("which item")) == nil {
		t.Error("pattern should still match")
	}
}

func TestMatchFromPatternDefaults(t *testing.T) {
	m := matchFromPattern(Pattern{"id": "x", "regex": "r"})
	if m.InputType != "multi_key" {
		t.Errorf("input_type default = %q", m.InputType)
	}
	if m.EOLPattern != `[\r\n]+` {
		t.Errorf("eol default = %q", m.EOLPattern)
	}
}
