//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"encoding/hex"
	"regexp"
	"strconv"
	"strings"

	"golang.org/x/crypto/blake2s"
)

// defaultPromptRegionTailLines mirrors _DEFAULT_PROMPT_REGION_TAIL_LINES.
const defaultPromptRegionTailLines = 12

// compiledPattern is a compiled positive regex plus its source pattern map and
// an optional precompiled negative-match regex.
type compiledPattern struct {
	re     *regexp.Regexp
	pat    Pattern
	neg    *regexp.Regexp
	negStr string
}

// PromptDetector performs cursor-aware prompt detection over normalized screen
// text. Faithful port of the Python class of the same name.
type PromptDetector struct {
	normalizer func(string) string
	patterns   []Pattern
	strict     bool

	compileFailures     []map[string]any
	compiledAll         []compiledPattern
	compiledNoCursorReq []compiledPattern
}

// DetectorOption configures a PromptDetector at construction.
type DetectorOption func(*PromptDetector)

// WithNormalizer sets the callback used to normalize prompt-region text for
// fingerprinting.
func WithNormalizer(fn func(string) string) DetectorOption {
	return func(d *PromptDetector) { d.normalizer = fn }
}

// WithStrict makes any pattern that fails to compile raise a
// *DetectorPatternCompileError from NewPromptDetector instead of being
// silently skipped.
func WithStrict(strict bool) DetectorOption {
	return func(d *PromptDetector) { d.strict = strict }
}

// NewPromptDetector builds a detector over patterns. In strict mode a bad
// pattern returns a *DetectorPatternCompileError; otherwise the error is
// always nil and bad patterns are recorded in CompileFailures.
func NewPromptDetector(patterns []Pattern, opts ...DetectorOption) (*PromptDetector, error) {
	d := &PromptDetector{patterns: patterns}
	for _, opt := range opts {
		opt(d)
	}
	compiled, err := d.compilePatterns()
	if err != nil {
		return nil, err
	}
	d.setCompiled(compiled)
	return d, nil
}

// mustDetector builds a non-strict detector (compile never errors) and is used
// by internal callers that supply their own patterns.
func mustDetector(patterns []Pattern) *PromptDetector {
	d, _ := NewPromptDetector(patterns)
	return d
}

func (d *PromptDetector) setCompiled(compiled []compiledPattern) {
	d.compiledAll = compiled
	d.compiledNoCursorReq = d.compiledNoCursorReq[:0]
	for _, cp := range compiled {
		if !patternBoolDefaultTrue(cp.pat, "expect_cursor_at_end") {
			d.compiledNoCursorReq = append(d.compiledNoCursorReq, cp)
		}
	}
}

// PatternCount returns the number of patterns held by the detector.
func (d *PromptDetector) PatternCount() int { return len(d.patterns) }

// CompileFailures returns a copy of the recorded pattern compile failures.
// Each entry carries "id", optionally "regex", and "error".
func (d *PromptDetector) CompileFailures() []map[string]any {
	out := make([]map[string]any, len(d.compileFailures))
	copy(out, d.compileFailures)
	return out
}

// PromptRegion extracts a bottom-of-content region likely to contain prompts,
// returning (regionText, cursorInRegion). It anchors to the last non-empty
// line, not the physical bottom row.
func PromptRegion(snapshot Snapshot, tailLines int) (string, bool) {
	screen, _ := snapshot["screen"].(string)
	if screen == "" {
		return "", false
	}
	lines := strings.Split(screen, "\n")
	lastIdx := 0
	for i := len(lines) - 1; i >= 0; i-- {
		if pyStrip(lines[i]) != "" {
			lastIdx = i
			break
		}
	}
	tail := tailLines
	if tail < 1 {
		tail = 1
	}
	startIdx := lastIdx - tail + 1
	if startIdx < 0 {
		startIdx = 0
	}
	cursorY := 0
	if cursor, ok := snapshot["cursor"].(map[string]any); ok {
		cursorY = pyIntOr0(cursor["y"])
	}
	cursorInRegion := startIdx <= cursorY && cursorY <= lastIdx
	regionText := strings.Join(lines[startIdx:lastIdx+1], "\n")
	return regionText, cursorInRegion
}

// NormalizePromptRegion normalizes volatile prompt-region fields for stable
// fingerprinting. An empty region is returned unchanged.
func NormalizePromptRegion(regionText string, normalizer func(string) string) string {
	if regionText == "" {
		return ""
	}
	if normalizer != nil {
		return normalizer(regionText)
	}
	return regionText
}

// PromptFingerprint computes a stable fingerprint for prompt-detection caching.
// Byte-compatible with the Python blake2s-based fingerprint.
func (d *PromptDetector) PromptFingerprint(snapshot Snapshot) string {
	region, _ := PromptRegion(snapshot, defaultPromptRegionTailLines)
	norm := NormalizePromptRegion(region, d.normalizer)
	sum := blake2s.Sum256([]byte(norm))
	h := hex.EncodeToString(sum[:])

	cursorAtEnd := boolToInt(patternBoolDefaultTrue(snapshot, "cursor_at_end"))
	trailing := boolToInt(pyTruthy(snapshot["has_trailing_space"]))

	cx, cy := 0, 0
	if cursor, ok := snapshot["cursor"].(map[string]any); ok {
		cx = pyIntOr0(cursor["x"])
		cy = pyIntOr0(cursor["y"])
	}
	return h + ":" + strconv.Itoa(cursorAtEnd) + ":" + strconv.Itoa(trailing) +
		":" + strconv.Itoa(cx) + ":" + strconv.Itoa(cy)
}

// resolveNegativeRegex extracts a negative-match regex string from a pattern.
// The bool reports whether a negative key was present (mirroring Python's
// None sentinel via a false return).
func resolveNegativeRegex(pattern Pattern) (string, bool) {
	if v, ok := pattern["negative_regex"]; ok {
		return asString(v), true
	}
	nm, ok := pattern["negative_match"].(map[string]any)
	if !ok {
		return "", false
	}
	sub := asString(nm["pattern"])
	switch asString(nm["match_mode"]) {
	case "contains":
		return pyRegexEscape(sub), true
	case "exact":
		return "^" + pyRegexEscape(sub) + "$", true
	default:
		return sub, true
	}
}

// AddPattern appends a new pattern and recompiles.
func (d *PromptDetector) AddPattern(pattern Pattern) error {
	return d.swapPatterns(append(append([]Pattern{}, d.patterns...), pattern))
}

// ReloadPatterns replaces all patterns and recompiles.
func (d *PromptDetector) ReloadPatterns(patterns []Pattern) error {
	return d.swapPatterns(append([]Pattern{}, patterns...))
}

func matchFromPattern(pattern Pattern) *PromptMatch {
	return &PromptMatch{
		PromptID:   asString(pattern["id"]),
		Pattern:    pattern,
		InputType:  patternString(pattern, "input_type", "multi_key"),
		EOLPattern: patternString(pattern, "eol_pattern", `[\r\n]+`),
		KVExtract:  pattern["kv_extract"],
	}
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
