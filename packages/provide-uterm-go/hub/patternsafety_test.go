//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"strings"
	"testing"
)

func TestValidatePatternSafetyRejectsRedosShapes(t *testing.T) {
	unsafe := []string{
		`(a+)+$`,       // nested quantified group
		`(?:a|aa)+`,    // alternation in quantified non-capturing group
		`(a|b)+x`,      // alternation in quantified capturing group
		`(?=(a+))+`,    // lookahead wrapping a quantified subgroup, itself quantified
		`(?:a|aa){2,}`, // counted-quantifier alternation
		`((a+))*`,      // nested quantified subgroup inside a star group
		`(a|b){2}`,     // counted quantifier over alternation
	}
	for _, p := range unsafe {
		if err := validatePatternSafety(p); err == nil {
			t.Fatalf("expected unsafe rejection for %q", p)
		} else if !strings.Contains(err.Error(), "unsafe watch pattern") {
			t.Fatalf("unexpected error for %q: %v", p, err)
		}
	}
}

func TestValidatePatternSafetyAcceptsStructurallySafe(t *testing.T) {
	// The validator (a pure scanner) accepts these, including lookaround and
	// named-group constructs it does not itself compile.
	safe := []string{
		`[abc]+`, `hello world`, `a+`, `(a|b)x`, `(ab)+`, `(a\|b)+`,
		`(?<=foo)bar`, `(?<!foo)bar`, `(?P<word>\w+)`, `(?P<a>b)`,
		`((a+)b)`, `((a|b)c)`, `foo|bar`, `(?i)abc`, `a{3}`, `a{`, `a{,5}`,
		`\(literal`, `[a-z`, `(ab){2}`,
	}
	for _, p := range safe {
		if err := validatePatternSafety(p); err != nil {
			t.Fatalf("expected safe for %q, got %v", p, err)
		}
	}
}

func TestCompilePatternLength(t *testing.T) {
	_, err := compilePattern(strings.Repeat("a", 9), 8)
	if err == nil || !strings.Contains(err.Error(), "watch pattern is too long: 9 > 8") {
		t.Fatalf("expected length error, got %v", err)
	}
}

func TestCompilePatternValid(t *testing.T) {
	re, err := compilePattern(`\d+`, defaultMaxPatternLength)
	mustTrue(t, err == nil && re != nil, "compiles")
	mustTrue(t, re.MatchString("abc123"), "matches")
}

func TestCompilePatternInvalidRegex(t *testing.T) {
	_, err := compilePattern(`[invalid`, defaultMaxPatternLength)
	mustTrue(t, err != nil && strings.Contains(err.Error(), "invalid watch pattern regex"), "invalid regex")
}

func TestCompilePatternRejectsUnsafe(t *testing.T) {
	_, err := compilePattern(`(a+)+$`, defaultMaxPatternLength)
	mustTrue(t, err != nil && strings.Contains(err.Error(), "unsafe watch pattern"), "unsafe rejected")
}

func TestCompilePatternRE2CompatibleSafePatterns(t *testing.T) {
	// The RE2-compilable subset of the structurally-safe list.
	for _, p := range []string{
		`[abc]+`, `hello world`, `a+`, `(a|b)x`, `(ab)+`, `(a\|b)+`,
		`(?P<word>\w+)`, `(?P<a>b)`, `((a+)b)`, `((a|b)c)`, `foo|bar`, `(?i)abc`,
	} {
		if _, err := compilePattern(p, defaultMaxPatternLength); err != nil {
			t.Fatalf("expected %q to compile, got %v", p, err)
		}
	}
}

func TestCompilePatternNamedGroupWithoutClose(t *testing.T) {
	// Validator passes (P prefix skip finds no '>'), RE2 then rejects it.
	_, err := compilePattern(`(?P abc`, defaultMaxPatternLength)
	mustTrue(t, err != nil && strings.Contains(err.Error(), "invalid watch pattern regex"), "malformed named group")
}

func TestCompilePatternZeroMaxLengthFloor(t *testing.T) {
	// maxPatternLength < 1 uses an effective floor of 1 for the comparison.
	_, err := compilePattern("ab", 0)
	mustTrue(t, err != nil && strings.Contains(err.Error(), "too long"), "floor of 1 applied")
	_, err = compilePattern("a", 0)
	mustTrue(t, err == nil, "single char within floor")
}

func TestLooksLikeCountedQuantifier(t *testing.T) {
	mustTrue(t, looksLikeCountedQuantifier("a{3}", 1), "{3}")
	mustTrue(t, looksLikeCountedQuantifier("a{2,}", 1), "{2,}")
	mustTrue(t, looksLikeCountedQuantifier("a{2,5}", 1), "{2,5}")
	mustFalse(t, looksLikeCountedQuantifier("a{", 1), "no close")
	mustFalse(t, looksLikeCountedQuantifier("a{}", 1), "empty body")
	mustFalse(t, looksLikeCountedQuantifier("a{,5}", 1), "no left digit")
	mustFalse(t, looksLikeCountedQuantifier("a{x}", 1), "non-digit")
	mustFalse(t, looksLikeCountedQuantifier("a{2,x}", 1), "non-digit right")
}

func TestIsDigits(t *testing.T) {
	mustFalse(t, isDigits(""), "empty")
	mustTrue(t, isDigits("123"), "digits")
	mustFalse(t, isDigits("12a"), "mixed")
}
