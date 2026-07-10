//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"encoding/json"
	"fmt"
	"regexp"
)

// maxExpectRegexLen is the maximum allowed prompt-guard regex length. Port of
// rest_helpers.MAX_EXPECT_REGEX_LEN.
const maxExpectRegexLen = 200

// PromptRegexError is returned when a prompt guard regex is invalid, unsafe, or
// too long. Port of rest_helpers.PromptRegexError. Kind is one of "too_long",
// "unsafe", "invalid".
type PromptRegexError struct {
	Message string
	Kind    string
}

func (e *PromptRegexError) Error() string { return e.Message }

// extractPromptID pulls prompt_detected.prompt_id from a snapshot, or "" when
// absent/malformed. Port of rest_helpers.extract_prompt_id (Go "" == Python None).
func extractPromptID(snapshot map[string]any) string {
	if len(snapshot) == 0 {
		return ""
	}
	prompt, ok := snapshot["prompt_detected"].(map[string]any)
	if !ok {
		return ""
	}
	value, ok := prompt["prompt_id"].(string)
	if ok && value != "" {
		return value
	}
	return ""
}

// compileExpectRegex compiles a prompt guard regex (with the IGNORECASE +
// MULTILINE flags applied via the (?im) prefix) or returns a *PromptRegexError.
// Port of rest_helpers.compile_expect_regex. An empty regex returns (nil, nil).
func compileExpectRegex(expectRegex string) (*regexp.Regexp, error) {
	if expectRegex == "" {
		return nil, nil
	}
	if len(expectRegex) > maxExpectRegexLen {
		return nil, &PromptRegexError{Message: "expect_regex too long", Kind: "too_long"}
	}
	if err := validatePatternSafety(expectRegex); err != nil {
		return nil, &PromptRegexError{Message: fmt.Sprintf("unsafe expect_regex: %s", err.Error()), Kind: "unsafe"}
	}
	re, err := regexp.Compile("(?im)" + expectRegex)
	if err != nil {
		return nil, &PromptRegexError{Message: fmt.Sprintf("invalid expect_regex: %s", err.Error()), Kind: "invalid"}
	}
	return re, nil
}

// snapshotMatches reports whether snapshot satisfies the prompt-id and/or regex
// guard. Port of rest_helpers.snapshot_matches.
func snapshotMatches(snapshot map[string]any, expectPromptID string, expectRegex *regexp.Regexp) bool {
	if snapshot == nil {
		return false
	}
	if expectPromptID != "" && extractPromptID(snapshot) != expectPromptID {
		return false
	}
	return expectRegex == nil || expectRegex.MatchString(toStr(snapshot["screen"]))
}

// toStr coerces a value to the string form Python's str() would produce for the
// inputs the ported code encounters: a string is returned verbatim, a missing
// (nil) value yields "", and anything else is fmt.Sprint'd.
func toStr(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprint(v)
}

// coerceFloat coerces an int/float value to float64, returning def otherwise.
// json.Number is handled because controlchannel decodes wire numbers into it
// (preserving the int/float text form for signature-faithful round-trips).
func coerceFloat(v any, def float64) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case int:
		return float64(n)
	case int64:
		return float64(n)
	case json.Number:
		f, err := n.Float64()
		if err != nil {
			return def
		}
		return f
	default:
		return def
	}
}
