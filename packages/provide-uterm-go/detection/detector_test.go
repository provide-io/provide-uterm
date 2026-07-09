//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
	"testing"
)

// snapFor mirrors the Python test conftest snap_factory.
func snapFor(screen string, cursorAtEnd bool, cursorY int) Snapshot {
	sum := sha256.Sum256([]byte(screen))
	return Snapshot{
		"screen":             screen,
		"screen_hash":        hex.EncodeToString(sum[:]),
		"cursor_at_end":      cursorAtEnd,
		"has_trailing_space": false,
		"cursor":             map[string]any{"y": cursorY, "x": 0},
		"captured_at":        1000.0,
	}
}

func snap(screen string) Snapshot {
	return snapFor(screen, true, strings.Count(screen, "\n"))
}

func makePatterns() []Pattern {
	return []Pattern{
		{"id": "prompt.login", "regex": `Enter your name:`, "input_type": "multi_key", "eol_pattern": "$"},
		{"id": "prompt.password", "regex": `Password:`, "input_type": "multi_key", "eol_pattern": "$"},
	}
}

func TestDetectPromptMatchesInRegion(t *testing.T) {
	d := mustDetector(makePatterns())
	match := d.DetectPrompt(snap("Welcome\nEnter your name:"))
	if match == nil || match.PromptID != "prompt.login" {
		t.Fatalf("match = %+v", match)
	}
	if match.EOLPattern != "$" {
		t.Errorf("eol = %q", match.EOLPattern)
	}
}

func TestDetectPromptNoMatch(t *testing.T) {
	d := mustDetector(makePatterns())
	if d.DetectPrompt(snap("Just some text")) != nil {
		t.Error("expected nil")
	}
}

func TestNegativeMatchExcludes(t *testing.T) {
	patterns := []Pattern{{
		"id": "prompt.buy", "regex": `which item`, "input_type": "single_key",
		"eol_pattern":    "$",
		"negative_match": map[string]any{"pattern": `stardock`, "match_mode": "regex"},
	}}
	d := mustDetector(patterns)
	if d.DetectPrompt(snap("stardock\nwhich item")) != nil {
		t.Error("negative should exclude")
	}
	if d.DetectPrompt(snap("shop\nwhich item")) == nil {
		t.Error("should match without negative text")
	}
	res := d.DetectPrompt(snap("regular store\nwhich item"))
	if res == nil || res.PromptID != "prompt.buy" {
		t.Error("negative absent should allow")
	}
}

func TestNegativeMatchCaseInsensitive(t *testing.T) {
	patterns := []Pattern{{
		"id": "p", "regex": `which item`, "input_type": "single_key",
		"negative_regex": "stardock",
	}}
	d := mustDetector(patterns)
	if d.DetectPrompt(snap("STARDOCK STATION\nwhich item")) != nil {
		t.Error("negative must be case-insensitive")
	}
}

func TestPositivePatternCaseSensitive(t *testing.T) {
	d := mustDetector(makePatterns())
	if d.DetectPrompt(snap("ENTER YOUR NAME:")) != nil {
		t.Error("positive patterns are case-sensitive by design")
	}
}

func TestNormalizerAffectsFingerprint(t *testing.T) {
	norm := func(s string) string { return strings.ReplaceAll(s, "name", "NAME") }
	d, err := NewPromptDetector(makePatterns(), WithNormalizer(norm))
	if err != nil {
		t.Fatal(err)
	}
	fp1 := d.PromptFingerprint(snap("Enter your name:"))
	fp2 := d.PromptFingerprint(snap("Enter your NAME:"))
	if fp1 != fp2 {
		t.Error("normalizer should collapse fingerprints")
	}
	plain := mustDetector(makePatterns())
	if plain.PromptFingerprint(snap("Enter your name:")) == plain.PromptFingerprint(snap("Enter your NAME:")) {
		t.Error("no normalizer should preserve difference")
	}
}

func TestDiagnosticsReturnsMatch(t *testing.T) {
	d := mustDetector(makePatterns())
	diag := d.DetectPromptWithDiagnostics(snap("Enter your name:"))
	if diag.Match == nil || diag.Match.PromptID != "prompt.login" {
		t.Error("diag match")
	}
	if d.DetectPromptWithDiagnostics(snap("nothing here")).Match != nil {
		t.Error("diag no match")
	}
}

func TestReloadAndAddPattern(t *testing.T) {
	d := mustDetector(nil)
	if d.DetectPrompt(snap("Enter your name:")) != nil {
		t.Error("empty detector should not match")
	}
	if err := d.ReloadPatterns(makePatterns()); err != nil {
		t.Fatal(err)
	}
	if d.DetectPrompt(snap("Enter your name:")) == nil {
		t.Error("reloaded patterns should match")
	}

	d2 := mustDetector(nil)
	if err := d2.AddPattern(Pattern{"id": "p.new", "regex": `New prompt`, "input_type": "single_key", "eol_pattern": "$"}); err != nil {
		t.Fatal(err)
	}
	if d2.DetectPrompt(snap("New prompt")) == nil {
		t.Error("added pattern should match")
	}
}

func TestPatternCount(t *testing.T) {
	d := mustDetector(makePatterns())
	if d.PatternCount() != 2 {
		t.Errorf("count = %d", d.PatternCount())
	}
	if err := d.AddPattern(Pattern{"id": "p.x", "regex": "X", "input_type": "single_key", "eol_pattern": "$"}); err != nil {
		t.Fatal(err)
	}
	if d.PatternCount() != 3 {
		t.Errorf("count = %d", d.PatternCount())
	}
}

// --- compile failures -------------------------------------------------------

func TestCompileBadRegexSkipped(t *testing.T) {
	d := mustDetector([]Pattern{
		{"id": "bad.pattern", "regex": `[invalid(`},
		{"id": "good.pattern", "regex": `Enter your name:`},
	})
	if d.PatternCount() != 2 {
		t.Error("both patterns retained")
	}
	if len(d.compiledAll) != 1 || patternID(d.compiledAll[0].pat) != "good.pattern" {
		t.Error("only good compiled")
	}
}

func TestCompileMissingRegexKeySkipped(t *testing.T) {
	d := mustDetector([]Pattern{
		{"id": "missing.regex"},
		{"id": "good.pattern", "regex": `Hello`},
	})
	if len(d.compiledAll) != 1 || patternID(d.compiledAll[0].pat) != "good.pattern" {
		t.Error("only good compiled")
	}
}

func TestCompileAllBadEmpty(t *testing.T) {
	d := mustDetector([]Pattern{{"id": "bad1", "regex": `[bad`}, {"id": "bad2", "regex": `(unclosed`}})
	if len(d.compiledAll) != 0 {
		t.Error("expected empty compiled")
	}
}

func TestCompileFailuresExposed(t *testing.T) {
	d := mustDetector([]Pattern{
		{"id": "bad.pattern", "regex": `[invalid(`},
		{"id": "missing.regex"},
		{"id": "good.pattern", "regex": `Enter your name:`},
	})
	failures := d.CompileFailures()
	if len(failures) != 2 {
		t.Fatalf("failures = %d", len(failures))
	}
	ids := map[string]bool{}
	for _, f := range failures {
		ids[asString(f["id"])] = true
	}
	if !ids["bad.pattern"] || !ids["missing.regex"] {
		t.Errorf("ids = %v", ids)
	}
	good := mustDetector([]Pattern{{"id": "good", "regex": `Hello`}})
	if len(good.CompileFailures()) != 0 {
		t.Error("expected no failures")
	}
}

func TestStrictModeRaises(t *testing.T) {
	_, err := NewPromptDetector([]Pattern{{"id": "bad.pattern", "regex": `[invalid(`}}, WithStrict(true))
	var ce *DetectorPatternCompileError
	if !errors.As(err, &ce) || !strings.Contains(err.Error(), "bad.pattern") {
		t.Fatalf("err = %v", err)
	}
	if len(ce.Failures) != 1 {
		t.Error("failure list")
	}
	_, err = NewPromptDetector([]Pattern{{"id": "missing.regex"}}, WithStrict(true))
	if err == nil || !strings.Contains(err.Error(), "missing.regex") {
		t.Errorf("missing key err = %v", err)
	}
}

func TestStrictModeAcceptsGood(t *testing.T) {
	d, err := NewPromptDetector([]Pattern{{"id": "good.pattern", "regex": `Enter your name:`}}, WithStrict(true))
	if err != nil {
		t.Fatal(err)
	}
	if d.PatternCount() != 1 || len(d.CompileFailures()) != 0 {
		t.Error("strict good detector")
	}
}

func TestStrictModeErrorIncludesCount(t *testing.T) {
	_, err := NewPromptDetector([]Pattern{
		{"id": "a", "regex": `[bad`},
		{"id": "b", "regex": `(unclosed`},
		{"id": "c", "regex": `\1invalidbackref`},
	}, WithStrict(true))
	if err == nil || !strings.Contains(err.Error(), "3 pattern(s) failed") {
		t.Errorf("err = %v", err)
	}
}

func TestStrictReloadRollsBack(t *testing.T) {
	d, err := NewPromptDetector([]Pattern{{"id": "good", "regex": `Hello`}}, WithStrict(true))
	if err != nil {
		t.Fatal(err)
	}
	if err := d.ReloadPatterns([]Pattern{{"id": "bad", "regex": `[oops`}}); err == nil {
		t.Fatal("expected reload error")
	}
	// old patterns still active
	if d.PatternCount() != 1 || d.DetectPrompt(snap("Hello")) == nil {
		t.Error("rollback failed")
	}
}

// --- prompt region -----------------------------------------------------------

func TestPromptRegionEmptyScreen(t *testing.T) {
	region, in := PromptRegion(Snapshot{"screen": "", "cursor": map[string]any{}}, defaultPromptRegionTailLines)
	if region != "" || in {
		t.Errorf("(%q, %v)", region, in)
	}
	// missing screen key entirely
	region, in = PromptRegion(Snapshot{}, defaultPromptRegionTailLines)
	if region != "" || in {
		t.Error("missing screen")
	}
}

func TestPromptRegionAllWhitespace(t *testing.T) {
	region, in := PromptRegion(Snapshot{"screen": "   \n   \n   ", "cursor": map[string]any{"y": 0}}, defaultPromptRegionTailLines)
	if region != "   " || !in {
		t.Errorf("(%q, %v)", region, in)
	}
}

func TestPromptRegionIgnoresTrailingBlankLines(t *testing.T) {
	region, in := PromptRegion(Snapshot{"screen": "top\nprompt>\n   \n\t", "cursor": map[string]any{"y": 1}}, 12)
	if region != "top\nprompt>" || !in {
		t.Errorf("(%q, %v)", region, in)
	}
}

func TestPromptRegionTailOne(t *testing.T) {
	region, in := PromptRegion(Snapshot{"screen": "old prompt\nactive prompt>\n   ", "cursor": map[string]any{"y": 1}}, 1)
	if region != "active prompt>" || !in {
		t.Errorf("(%q, %v)", region, in)
	}
	region, in = PromptRegion(Snapshot{"screen": "line 0\nline 1\nline 2", "cursor": map[string]any{"y": 2}}, 1)
	if region != "line 2" || !in {
		t.Errorf("(%q, %v)", region, in)
	}
	// tail below 1 clamps to 1
	region, _ = PromptRegion(Snapshot{"screen": "a\nb", "cursor": map[string]any{"y": 1}}, 0)
	if region != "b" {
		t.Errorf("clamp: %q", region)
	}
}

func TestPromptRegionCursorBoundaries(t *testing.T) {
	screen := "line 0\nline 1\nline 2"
	region, in := PromptRegion(Snapshot{"screen": screen, "cursor": map[string]any{"y": 1}}, 2)
	if region != "line 1\nline 2" || !in {
		t.Error("start boundary")
	}
	_, in = PromptRegion(Snapshot{"screen": screen, "cursor": map[string]any{"y": 2}}, 2)
	if !in {
		t.Error("last boundary")
	}
	_, in = PromptRegion(Snapshot{"screen": screen, "cursor": map[string]any{"y": 0}}, 2)
	if in {
		t.Error("cursor above region")
	}
}

func TestPromptRegionBadCursor(t *testing.T) {
	region, in := PromptRegion(Snapshot{"screen": "Hello\nWorld", "cursor": map[string]any{"y": "not_an_int"}}, defaultPromptRegionTailLines)
	if region != "Hello\nWorld" || !in {
		t.Errorf("(%q, %v)", region, in)
	}
	// no cursor key at all -> y=0
	region, in = PromptRegion(Snapshot{"screen": "Only prompt"}, defaultPromptRegionTailLines)
	if region != "Only prompt" || !in {
		t.Error("missing cursor")
	}
}

// --- normalize / fingerprint -------------------------------------------------

func TestNormalizePromptRegion(t *testing.T) {
	if NormalizePromptRegion("Hello World", func(string) string { return "NORMALIZED" }) != "NORMALIZED" {
		t.Error("normalizer applied")
	}
	if NormalizePromptRegion("", func(string) string { return "NEVER" }) != "" {
		t.Error("empty short-circuits")
	}
	if NormalizePromptRegion("as-is", nil) != "as-is" {
		t.Error("nil normalizer")
	}
}

func TestFingerprintIncludesCursorCoords(t *testing.T) {
	d := mustDetector(nil)
	s := snap("some text")
	s["cursor"] = map[string]any{"x": 10, "y": 20}
	if !strings.HasSuffix(d.PromptFingerprint(s), ":10:20") {
		t.Error("cursor coords")
	}
	s["cursor"] = map[string]any{"x": "bad", "y": nil}
	if !strings.HasSuffix(d.PromptFingerprint(s), ":0:0") {
		t.Error("bad cursor defaults")
	}
	// missing cursor key
	delete(s, "cursor")
	if !strings.HasSuffix(d.PromptFingerprint(s), ":0:0") {
		t.Error("missing cursor")
	}
}

func TestFingerprintFlags(t *testing.T) {
	d := mustDetector(nil)
	s := snap("text")
	s["cursor_at_end"] = false
	s["has_trailing_space"] = true
	fp := d.PromptFingerprint(s)
	parts := strings.Split(fp, ":")
	if len(parts) != 5 || parts[1] != "0" || parts[2] != "1" {
		t.Errorf("fp = %q", fp)
	}
}

// --- resolveNegativeRegex ----------------------------------------------------

func TestResolveNegativeRegex(t *testing.T) {
	if got, ok := resolveNegativeRegex(Pattern{"negative_regex": "stardock"}); !ok || got != "stardock" {
		t.Error("negative_regex key")
	}
	if got, ok := resolveNegativeRegex(Pattern{"negative_match": map[string]any{"pattern": "stardock.station", "match_mode": "contains"}}); !ok || got != `stardock\.station` {
		t.Errorf("contains got %q", got)
	}
	if got, ok := resolveNegativeRegex(Pattern{"negative_match": map[string]any{"pattern": "Stardock", "match_mode": "exact"}}); !ok || got != `^Stardock$` {
		t.Errorf("exact got %q", got)
	}
	if got, ok := resolveNegativeRegex(Pattern{"negative_match": map[string]any{"pattern": `star\w+`, "match_mode": "regex"}}); !ok || got != `star\w+` {
		t.Errorf("regex got %q", got)
	}
	if got, ok := resolveNegativeRegex(Pattern{"negative_match": map[string]any{"pattern": `star\w+`}}); !ok || got != `star\w+` {
		t.Errorf("default mode got %q", got)
	}
	if got, ok := resolveNegativeRegex(Pattern{"negative_match": map[string]any{"match_mode": "regex"}}); !ok || got != "" {
		t.Errorf("missing pattern got %q ok=%v", got, ok)
	}
	if _, ok := resolveNegativeRegex(Pattern{"negative_match": "banner"}); ok {
		t.Error("non-dict negative_match should be none")
	}
	if _, ok := resolveNegativeRegex(Pattern{"id": "foo", "regex": "X"}); ok {
		t.Error("absent should be none")
	}
}
