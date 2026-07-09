//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

// detectInText scans compiled patterns in order against text, honoring
// negative-match exclusions and the per-pattern cursor-at-end requirement.
// The first surviving pattern wins. When cursorMiss is non-nil, patterns that
// matched but failed only the cursor check are recorded as fallback
// candidates.
func (d *PromptDetector) detectInText(
	text, fullScreen string,
	cursorAtEnd bool,
	compiled []compiledPattern,
	failed *[]map[string]any,
	cursorMiss *[]*PromptMatch,
) *PromptMatch {
	for _, cp := range compiled {
		if !cp.re.MatchString(text) {
			continue
		}
		// negative_match is intentionally case-insensitive; positive patterns
		// are case-sensitive by design.
		if cp.neg != nil && cp.neg.MatchString(fullScreen) {
			*failed = append(*failed, map[string]any{
				"pattern_id":       patternID(cp.pat),
				"reason":           "negative_match",
				"negative_pattern": cp.negStr,
			})
			continue
		}
		expectCursorAtEnd := patternBoolDefaultTrue(cp.pat, "expect_cursor_at_end")
		if expectCursorAtEnd && !cursorAtEnd {
			*failed = append(*failed, map[string]any{
				"pattern_id":             patternID(cp.pat),
				"reason":                 "cursor_position",
				"expected_cursor_at_end": expectCursorAtEnd,
				"actual_cursor_at_end":   cursorAtEnd,
			})
			if cursorMiss != nil {
				*cursorMiss = append(*cursorMiss, matchFromPattern(cp.pat))
			}
			continue
		}
		return matchFromPattern(cp.pat)
	}
	return nil
}

// runTwoPass runs prompt-region detection first, then a full-screen fallback
// when the cursor is not within the region.
func (d *PromptDetector) runTwoPass(
	snapshot Snapshot,
	screen string,
	cursorAtEnd bool,
	compiledFast, compiledAll []compiledPattern,
	failed *[]map[string]any,
) (*PromptMatch, []*PromptMatch) {
	var cursorMiss []*PromptMatch
	regionText, cursorInRegion := PromptRegion(snapshot, defaultPromptRegionTailLines)
	if regionText != "" {
		if m := d.detectInText(regionText, screen, cursorAtEnd, compiledFast, failed, nil); m != nil {
			return m, cursorMiss
		}
	}
	if !cursorInRegion {
		if m := d.detectInText(screen, screen, cursorAtEnd, compiledAll, failed, &cursorMiss); m != nil {
			return m, cursorMiss
		}
	}
	return nil, cursorMiss
}

// DetectPromptWithDiagnostics detects a prompt and returns partial-match
// diagnostics alongside the match.
func (d *PromptDetector) DetectPromptWithDiagnostics(snapshot Snapshot) PromptDetectionDiagnostics {
	screen, _ := snapshot["screen"].(string)
	cursorAtEnd := patternBoolDefaultTrue(snapshot, "cursor_at_end")
	hasTrailingSpace := pyTruthy(snapshot["has_trailing_space"])

	failed := []map[string]any{}

	compiledAll := d.compiledAll
	compiledFast := compiledAll
	if !cursorAtEnd {
		compiledFast = d.compiledNoCursorReq
	}

	match, cursorMiss := d.runTwoPass(snapshot, screen, cursorAtEnd, compiledFast, compiledAll, &failed)
	if match != nil {
		return PromptDetectionDiagnostics{Match: match, RegexMatchedButFailed: failed}
	}

	// Fallback: matched the regex but the cursor heuristic disagreed. Prefer
	// progress when trailing space strongly correlates with an active field.
	if len(cursorMiss) > 0 && hasTrailingSpace {
		return PromptDetectionDiagnostics{Match: cursorMiss[0], RegexMatchedButFailed: failed}
	}

	return PromptDetectionDiagnostics{Match: nil, RegexMatchedButFailed: failed}
}

// DetectPrompt detects whether the snapshot contains a prompt awaiting input,
// returning only the match (nil when none).
func (d *PromptDetector) DetectPrompt(snapshot Snapshot) *PromptMatch {
	return d.DetectPromptWithDiagnostics(snapshot).Match
}
