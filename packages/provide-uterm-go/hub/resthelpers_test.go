//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestExtractPromptID(t *testing.T) {
	mustEqual(t, extractPromptID(nil), "", "nil snapshot")
	mustEqual(t, extractPromptID(map[string]any{}), "", "empty snapshot")
	mustEqual(t, extractPromptID(map[string]any{"prompt_detected": "notdict"}), "", "non-dict prompt")
	mustEqual(t, extractPromptID(map[string]any{"prompt_detected": map[string]any{}}), "", "no prompt_id")
	mustEqual(t, extractPromptID(map[string]any{"prompt_detected": map[string]any{"prompt_id": ""}}), "", "empty prompt_id")
	mustEqual(t, extractPromptID(map[string]any{"prompt_detected": map[string]any{"prompt_id": 42}}), "", "non-string prompt_id")
	mustEqual(t, extractPromptID(map[string]any{"prompt_detected": map[string]any{"prompt_id": "menu"}}), "menu", "valid")
}

func TestCompileExpectRegex(t *testing.T) {
	re, err := compileExpectRegex("")
	mustTrue(t, re == nil && err == nil, "empty -> nil")

	_, err = compileExpectRegex(strings.Repeat("a", maxExpectRegexLen+1))
	var pre *PromptRegexError
	mustTrue(t, asError(err, &pre) && pre.Kind == "too_long", "too long")

	_, err = compileExpectRegex("(a+)+$")
	mustTrue(t, asError(err, &pre) && pre.Kind == "unsafe", "unsafe")
	mustTrue(t, strings.Contains(err.Error(), "unsafe expect_regex"), "unsafe message")

	_, err = compileExpectRegex("(unclosed")
	mustTrue(t, asError(err, &pre) && pre.Kind == "invalid", "invalid")
	mustTrue(t, strings.Contains(err.Error(), "invalid expect_regex"), "invalid message")

	re, err = compileExpectRegex("^ab+c")
	mustTrue(t, err == nil && re != nil, "valid")
	mustTrue(t, re.MatchString("XX\nABBBC"), "ignorecase + multiline")
}

func TestSnapshotMatches(t *testing.T) {
	mustFalse(t, snapshotMatches(nil, "", nil), "nil")

	snap := map[string]any{"prompt_detected": map[string]any{"prompt_id": "menu"}, "screen": "Menu here"}
	mustTrue(t, snapshotMatches(snap, "menu", nil), "prompt id match")
	mustFalse(t, snapshotMatches(snap, "other", nil), "prompt id mismatch")

	re, _ := compileExpectRegex("menu")
	mustTrue(t, snapshotMatches(snap, "", re), "regex match on screen")
	reNo, _ := compileExpectRegex("WONTMATCH")
	mustFalse(t, snapshotMatches(snap, "", reNo), "regex no match")

	// No screen key -> "" -> regex must not match a non-empty pattern.
	mustFalse(t, snapshotMatches(map[string]any{}, "", reNo), "empty screen")
}

func TestToStr(t *testing.T) {
	mustEqual(t, toStr(nil), "", "nil -> empty")
	mustEqual(t, toStr("hi"), "hi", "string passthrough")
	mustEqual(t, toStr(42), "42", "int stringified")
}

func TestCoerceFloat(t *testing.T) {
	mustEqual(t, coerceFloat(1.5, 0), 1.5, "float")
	mustEqual(t, coerceFloat(3, 0), 3.0, "int")
	mustEqual(t, coerceFloat(int64(4), 0), 4.0, "int64")
	mustEqual(t, coerceFloat("x", 9), 9.0, "default for non-numeric")
	mustEqual(t, coerceFloat(nil, 7), 7.0, "default for nil")
	// controlchannel UseNumber path
	mustEqual(t, coerceFloat(json.Number("2.5"), 0), 2.5, "json.Number")
	mustEqual(t, coerceFloat(json.Number("nope"), 8), 8.0, "bad json.Number")
}

// TestFrameToMapEncodeError covers the EncodeFrame failure arm.
func TestFrameToMapEncodeError(t *testing.T) {
	if _, err := frameToMap(42); err == nil {
		t.Fatal("frameToMap(42) should fail")
	}
}

func TestCoerceSeq(t *testing.T) {
	mustEqual(t, coerceSeq(map[string]any{}), 0, "missing -> 0")
	mustEqual(t, coerceSeq(map[string]any{"seq": 5}), 5, "int")
	mustEqual(t, coerceSeq(map[string]any{"seq": int64(6)}), 6, "int64")
	mustEqual(t, coerceSeq(map[string]any{"seq": 7.0}), 7, "float")
	mustEqual(t, coerceSeq(map[string]any{"seq": "x"}), 0, "non-numeric -> 0")
}

func TestPromptRegexErrorString(t *testing.T) {
	e := &PromptRegexError{Message: "boom", Kind: "invalid"}
	mustEqual(t, e.Error(), "boom", "error string")
}
