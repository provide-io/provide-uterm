//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"fmt"
	"regexp"
	"strings"
)

// Default bounds for watch-pattern compilation. Port of the module constants
// in provide.uterm.server.bridge.hub.event_bus.
const (
	defaultMaxPatternLength   = 512
	defaultMaxMatchInputChars = 8192
)

// compilePattern validates and compiles a watch/guard regex. Port of
// event_bus._compile_pattern. Empty maxPatternLength defaults to 1 for the
// comparison (matching Python's max(1, int(...))), but the error message
// reports the raw configured length.
//
// Deviation: Go's regexp is RE2, which rejects the lookaround / backreference
// constructs Python's `re` accepts. The ReDoS-safety validator below is ported
// verbatim, but a lookbehind/lookahead pattern that passes the validator will
// still fail to compile here where Python would succeed.
func compilePattern(pattern string, maxPatternLength int) (*regexp.Regexp, error) {
	effectiveMax := maxPatternLength
	if effectiveMax < 1 {
		effectiveMax = 1
	}
	if len(pattern) > effectiveMax {
		return nil, fmt.Errorf("watch pattern is too long: %d > %d", len(pattern), maxPatternLength)
	}
	if err := validatePatternSafety(pattern); err != nil {
		return nil, err
	}
	re, err := regexp.Compile(pattern)
	if err != nil {
		return nil, fmt.Errorf("invalid watch pattern regex: %w", err)
	}
	return re, nil
}

// validatePatternSafety scans pattern for ReDoS shapes (nested quantified
// groups, quantified alternation groups) and returns an error naming the
// offending shape. Port of event_bus._validate_pattern_safety — a pure string
// scanner independent of any regex engine.
func validatePatternSafety(pattern string) error {
	// Each frame is [hasInnerQuantifier, hasAlternation] for a group.
	var groupStack [][2]bool
	previousKind := ""
	lastClosedGroupHadQuantifier := false
	lastClosedGroupHadAlternation := false
	escaped := false
	inClass := false
	i := 0
	n := len(pattern)
	for i < n {
		char := pattern[i]
		if escaped {
			escaped = false
			previousKind = "literal"
			i++
			continue
		}
		if char == '\\' {
			escaped = true
			i++
			continue
		}
		if inClass {
			if char == ']' {
				inClass = false
				previousKind = "literal"
			}
			i++
			continue
		}
		if char == '[' {
			inClass = true
			i++
			continue
		}
		if char == '(' {
			groupStack = append(groupStack, [2]bool{false, false})
			previousKind = ""
			lastClosedGroupHadQuantifier = false
			lastClosedGroupHadAlternation = false
			i++
			i = skipGroupPrefix(pattern, i, n)
			continue
		}
		if char == ')' && len(groupStack) > 0 {
			frame := groupStack[len(groupStack)-1]
			groupStack = groupStack[:len(groupStack)-1]
			lastClosedGroupHadQuantifier = frame[0]
			lastClosedGroupHadAlternation = frame[1]
			if len(groupStack) > 0 {
				if frame[0] {
					groupStack[len(groupStack)-1][0] = true
				}
				if frame[1] {
					groupStack[len(groupStack)-1][1] = true
				}
			}
			previousKind = "group"
			i++
			continue
		}
		if char == '|' {
			if len(groupStack) > 0 {
				groupStack[len(groupStack)-1][1] = true
			}
			previousKind = "alternation"
			i++
			continue
		}
		if char == '+' || char == '*' || (char == '{' && looksLikeCountedQuantifier(pattern, i)) {
			if previousKind == "group" {
				if lastClosedGroupHadQuantifier {
					return fmt.Errorf("unsafe watch pattern: nested quantified groups are not allowed")
				}
				if lastClosedGroupHadAlternation {
					return fmt.Errorf("unsafe watch pattern: quantified groups containing alternation are not allowed")
				}
			}
			if len(groupStack) > 0 {
				groupStack[len(groupStack)-1][0] = true
			}
			previousKind = "quantifier"
			if char == '{' {
				// looksLikeCountedQuantifier guaranteed a closing '}' at/after i.
				i = i + strings.IndexByte(pattern[i:], '}') + 1
			} else {
				i++
			}
			continue
		}
		previousKind = "literal"
		lastClosedGroupHadQuantifier = false
		lastClosedGroupHadAlternation = false
		i++
	}
	return nil
}

// skipGroupPrefix advances i past a group-prefix metadata sequence like (?:,
// (?=, (?!, (?<=, (?<!, or (?P<name>, so those non-content characters do not
// affect quantifier/alternation tracking. i points just past the '('.
func skipGroupPrefix(pattern string, i, n int) int {
	if i >= n || pattern[i] != '?' {
		return i
	}
	i++ // past '?'
	switch {
	case i < n && pattern[i] == '<' && i+1 < n && (pattern[i+1] == '=' || pattern[i+1] == '!'):
		// (?<= or (?<! lookbehind.
		i += 2
	case i < n && pattern[i] == 'P':
		// (?P<name>: skip to '>'.
		if end := strings.IndexByte(pattern[i:], '>'); end != -1 {
			i = i + end + 1
		}
	case i < n && (pattern[i] == ':' || pattern[i] == '=' || pattern[i] == '!'):
		// (?:, (?=, (?!: single marker char.
		i++
	}
	// else: unknown / inline flag like (?i); leave i alone.
	return i
}

// looksLikeCountedQuantifier reports whether pattern[start] begins a counted
// quantifier like {2}, {2,}, or {2,5}. Port of _looks_like_counted_quantifier.
func looksLikeCountedQuantifier(pattern string, start int) bool {
	rel := strings.IndexByte(pattern[start+1:], '}')
	if rel == -1 {
		return false
	}
	end := start + 1 + rel
	body := pattern[start+1 : end]
	if body == "" {
		return false
	}
	left, right, found := strings.Cut(body, ",")
	return isDigits(left) && (!found || right == "" || isDigits(right))
}

// isDigits reports whether s is non-empty and all ASCII digits (str.isdigit for
// the digit-only inputs this port encounters).
func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}
