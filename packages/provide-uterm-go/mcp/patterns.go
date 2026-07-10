//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import "regexp"

// Structural ReDoS guard for untrusted (LLM/user-supplied) regex patterns.
// Port of provide.uterm.ai.patterns.has_catastrophic_construct.
//
// Detects the cheap, well-known catastrophic constructs before compilation:
//   - nested quantifiers — a quantified group whose body is itself quantified
//     (e.g. "(a+)+"), the classic exponential-backtracking shape; and
//   - quantified backreferences — "\1+", "(\1)+" …
//
// This is a structural denylist, not a proof of linear-time matching; see the
// Python module docstring for the residual-risk caveats.

// backrefRE matches a backreference token: \1 .. \99.
var backrefRE = regexp.MustCompile(`^\\[1-9][0-9]?$`)

// quantifiedBackrefRE matches a quantified backreference anywhere in the
// pattern: \1+, \2*, \3{...}.
var quantifiedBackrefRE = regexp.MustCompile(`\\[1-9][0-9]?[+*{]`)

// isQuantifierOpener reports whether c, when it immediately follows a group's
// closing paren, makes that group "repeated".
func isQuantifierOpener(c byte) bool {
	return c == '+' || c == '*' || c == '{'
}

// groupBodyAfter pairs a balanced-group body with the character following its
// closing paren.
type groupBodyAfter struct {
	body  string
	after string
}

// groupBodiesWithFollowingChar returns (body, char_after_close) for every
// balanced "(...)" group. Escaped parens are treated as literals; unbalanced
// closes are ignored (the downstream compiler rejects the pattern as invalid),
// so this never panics on malformed input.
func groupBodiesWithFollowingChar(pattern string) []groupBodyAfter {
	var stack []int
	var results []groupBodyAfter
	i, n := 0, len(pattern)
	for i < n {
		c := pattern[i]
		if c == '\\' {
			// Skip the escaped character so "\(" / "\)" are literals.
			i += 2
			continue
		}
		switch {
		case c == '(':
			stack = append(stack, i)
		case c == ')' && len(stack) > 0:
			start := stack[len(stack)-1]
			stack = stack[:len(stack)-1]
			body := pattern[start+1 : i]
			after := ""
			if i+1 < n {
				after = pattern[i+1 : i+2]
			}
			results = append(results, groupBodyAfter{body: body, after: after})
		}
		i++
	}
	return results
}

// bodyIsRepeatedUnit reports whether a group body is itself a repeated/backref
// unit. Such a body, when the enclosing group is also quantified, forms the
// classic nested-quantifier or quantified-backref-in-group catastrophic shape.
func bodyIsRepeatedUnit(body string) bool {
	if body == "" {
		return false
	}
	// The whole body is a backreference, e.g. "(\1)+".
	if backrefRE.MatchString(body) {
		return true
	}
	last := body[len(body)-1]
	// A lazy quantifier ("a+?") ends in "?"; the real quantifier precedes it.
	if last == '?' {
		return len(body) >= 2 && (body[len(body)-2] == '+' || body[len(body)-2] == '*' || body[len(body)-2] == '}')
	}
	if last != '+' && last != '*' && last != '}' {
		return false
	}
	// Guard against an escaped quantifier ("a\*") being read as a quantifier.
	return len(body) < 2 || body[len(body)-2] != '\\'
}

// hasCatastrophicConstruct reports whether pattern contains a known
// catastrophic construct (nested quantifier or quantified backreference).
func hasCatastrophicConstruct(pattern string) bool {
	if quantifiedBackrefRE.MatchString(pattern) {
		return true
	}
	for _, g := range groupBodiesWithFollowingChar(pattern) {
		if g.after != "" && isQuantifierOpener(g.after[0]) && bodyIsRepeatedUnit(g.body) {
			return true
		}
	}
	return false
}
