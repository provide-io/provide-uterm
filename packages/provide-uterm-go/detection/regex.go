//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"fmt"
	"regexp"
	"strings"
)

// RE2TranslateError is returned when a Python-syntax regex cannot be compiled
// under Go's RE2 engine, even after the trivial translations applied by
// translatePyRegex. It names the offending pattern so callers can surface a
// precise diagnostic instead of silently dropping the rule.
//
// The common causes are constructs RE2 does not support: lookaround
// ((?=...), (?!...), (?<=...), (?<!...)) and backreferences (\1, (?P=name)).
type RE2TranslateError struct {
	// Pattern is the (translated) Python regex string that failed to compile.
	Pattern string
	// Err is the underlying regexp compile error.
	Err error
}

func (e *RE2TranslateError) Error() string {
	return fmt.Sprintf("regex %q is not RE2-compatible: %v", e.Pattern, e.Err)
}

func (e *RE2TranslateError) Unwrap() error { return e.Err }

// translatePyRegex rewrites the trivial Python-regex constructs that differ
// from RE2 into their RE2 spelling. Currently that is the single mechanical
// case “\Z“ (Python "end of string") -> “\z“ (RE2 "end of text"); the two
// have identical semantics. Escaped backslashes (“\\Z“) and character-class
// contents are left untouched. Inline flags such as “(?i)“ already parse
// under RE2, so they need no translation.
func translatePyRegex(pattern string) string {
	if !strings.Contains(pattern, `\Z`) {
		return pattern
	}
	var b strings.Builder
	b.Grow(len(pattern))
	rs := []rune(pattern)
	for i := 0; i < len(rs); i++ {
		if rs[i] == '\\' && i+1 < len(rs) {
			next := rs[i+1]
			if next == 'Z' {
				b.WriteString(`\z`)
				i++
				continue
			}
			// Preserve any other escape verbatim (including \\ so a
			// following Z is treated as a literal, not our anchor).
			b.WriteRune('\\')
			b.WriteRune(next)
			i++
			continue
		}
		b.WriteRune(rs[i])
	}
	return b.String()
}

// compilePyRegex compiles a Python-syntax regex under RE2 with the given
// inline flags (e.g. "m" for MULTILINE, "mi" for MULTILINE|IGNORECASE). It
// applies translatePyRegex first and wraps any residual compile failure in a
// *RE2TranslateError.
func compilePyRegex(pattern, flags string) (*regexp.Regexp, error) {
	translated := translatePyRegex(pattern)
	src := translated
	if flags != "" {
		src = "(?" + flags + ")" + translated
	}
	re, err := regexp.Compile(src)
	if err != nil {
		return nil, &RE2TranslateError{Pattern: translated, Err: err}
	}
	return re, nil
}

// CheckRuleSetRE2 verifies every regex a RuleSet can produce — positive
// matches, negative matches, and kv_extract patterns — compiles under RE2
// (after translatePyRegex). It returns one human-readable entry per offending
// pattern, each naming the prompt id, the pattern role, the regex, and the
// compile error; an empty slice means the ruleset is fully RE2-compatible.
func CheckRuleSetRE2(rs *RuleSet) []string {
	var bad []string
	check := func(promptID, role, pattern, flags string) {
		if _, err := compilePyRegex(pattern, flags); err != nil {
			bad = append(bad, fmt.Sprintf("prompt %q %s: %v", promptID, role, err))
		}
	}
	for _, prompt := range rs.Prompts {
		if regex, ok := prompt.Match.ToRegex(); ok {
			check(prompt.ID, "match", regex, "m")
		}
		if prompt.NegativeMatch != nil {
			if regex, ok := prompt.NegativeMatch.ToRegex(); ok {
				check(prompt.ID, "negative_match", regex, "mi")
			}
		}
		for _, kv := range prompt.KVExtract {
			check(prompt.ID, fmt.Sprintf("kv_extract[%s]", kv.Field), kv.Regex, "mi")
		}
	}
	return bad
}

// Special characters escaped by CPython's re.escape (its _special_chars_map):
//
//	()[]{}?*+-|^$\.&~#   plus the whitespace bytes  space \t \n \r \v \f
//
// Reproduced exactly so RuleSet.to_prompt_patterns() emits byte-identical
// regex strings to the Python implementation (the round-trip tests compare
// these strings). Every escaped form compiles cleanly under RE2.
var pyEscapeSet = map[rune]struct{}{
	'(': {}, ')': {}, '[': {}, ']': {}, '{': {}, '}': {},
	'?': {}, '*': {}, '+': {}, '-': {}, '|': {}, '^': {},
	'$': {}, '\\': {}, '.': {}, '&': {}, '~': {}, '#': {},
	' ': {}, '\t': {}, '\n': {}, '\r': {}, '\v': {}, '\f': {},
}

// pyRegexEscape mirrors Python's re.escape: every character in pyEscapeSet is
// prefixed with a backslash; all others pass through unchanged.
func pyRegexEscape(s string) string {
	var b strings.Builder
	b.Grow(len(s) + 8)
	for _, r := range s {
		if _, ok := pyEscapeSet[r]; ok {
			b.WriteRune('\\')
		}
		b.WriteRune(r)
	}
	return b.String()
}
