//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

// Snapshot is the screen-snapshot dict passed to the detector/engine. It is a
// map (mirroring the Python ScreenSnapshot TypedDict) so callers can supply
// exactly the keys the reference implementation reads:
//
//	screen (string, required), screen_hash (string, required),
//	cursor_at_end (bool), has_trailing_space (bool),
//	cursor (map with "x"/"y"), captured_at (float64).
type Snapshot = map[string]any

// Pattern is a single prompt pattern dictionary, as produced by
// RuleSet.ToPromptPatterns or supplied directly by callers/tests. Recognized
// keys: id, regex, input_type, expect_cursor_at_end, notes, auto_detected,
// negative_regex, negative_match, kv_extract, eol_pattern.
type Pattern = map[string]any

// PromptMatch is a matched prompt pattern with its rule metadata. Faithful to
// the Python pydantic model of the same name.
type PromptMatch struct {
	PromptID   string  `json:"prompt_id"`
	Pattern    Pattern `json:"pattern"`
	InputType  string  `json:"input_type"`
	EOLPattern string  `json:"eol_pattern"`
	// KVExtract is the raw kv_extract config (a list, a dict, or nil).
	KVExtract any `json:"kv_extract"`
}

// PromptDetection is the complete prompt-detection result.
type PromptDetection struct {
	PromptID  string         `json:"prompt_id"`
	InputType string         `json:"input_type"`
	KVData    map[string]any `json:"kv_data"`
	Match     *PromptMatch   `json:"match"`
	// IsIdle is nil until set by the async processing path; a set value is a
	// pointer to the boolean, matching Python's Optional[bool].
	IsIdle *bool         `json:"is_idle"`
	Buffer *ScreenBuffer `json:"buffer"`
}

// PromptDetectionDiagnostics is a detection result plus partial-match
// diagnostics for debugging.
type PromptDetectionDiagnostics struct {
	Match                 *PromptMatch     `json:"match"`
	RegexMatchedButFailed []map[string]any `json:"regex_matched_but_failed"`
}
