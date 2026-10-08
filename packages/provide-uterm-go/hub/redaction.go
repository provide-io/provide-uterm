//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"
)

// defaultReplacement mirrors the Pydantic default of ext.RedactionRule.replacement
// ("[REDACTED]"). A [RedactionRule] with an empty Replacement is treated as this
// value, so a rule constructed with only a pattern still redacts to a visible
// marker (Go structs cannot carry field defaults, so the default is applied here
// at redactor-build time rather than at rule construction).
const defaultReplacement = "[REDACTED]"

// StreamRedactor is a high-performance regex-based stream redactor. Port of
// provide.uterm.server.bridge.hub.redaction.StreamRedactor.
//
// Every rule pattern is wrapped in a top-level capturing group and the groups
// are joined with "|" into a single combined regexp, so redaction is a single
// pass. When all rules share one replacement string the fast single-replacement
// path is used; otherwise the matched rule is identified by replicating Python's
// re.Match.lastindex (the highest-numbered capturing group that participated in
// the match) and a bisect over the per-rule start-group indices.
//
// Lookahead: RE2 has none, but a rule whose pattern ends in one — `(?=X)` with
// nothing after it but closing parentheses, as the default generic password/
// api_key/token rules do — is honoured anyway. The lookahead becomes a
// capturing group; a match must then be followed by X, as in Python, and the
// text X matched is neither replaced nor consumed: it is copied through and
// the search resumes at its start. (Resuming searches the rest of the input
// on its own, so an assertion at that very point — \b, ^ — sees the start of
// text rather than the character before it. No default rule can begin there,
// since each starts with a word character or "-" and every default lookahead
// matches only whitespace, punctuation or the end.)
//
// Any other construct RE2 rejects (lookbehind, a lookahead with more pattern
// after it, backreferences) fails to compile and the rule is SKIPPED, exactly
// like the Python invalid-regex path (`except re.error: continue`). Skipped
// rules simply do not redact; the remaining rules are unaffected.
type StreamRedactor struct {
	pattern           *regexp.Regexp
	ruleStartIndices  []int
	replacements      []string
	singleReplacement *string
	// lookaheadGroups holds, per rule, the combined pattern's group index of
	// the rule's tail lookahead, or 0 when it has none. nil when no rule has
	// one, which keeps the original single-pass paths.
	lookaheadGroups []int
}

// tailLookahead rewrites a pattern ending in a lookahead `(?=X)` (followed by
// nothing but closing parentheses) so the lookahead is the capturing group
// named name. ok is false when the pattern has no such lookahead.
func tailLookahead(pattern, name string) (string, bool) {
	start := strings.LastIndex(pattern, "(?=")
	if start < 0 || escaped(pattern, start) {
		return pattern, false
	}
	depth := 0
	inClass := false
	for i := start; i < len(pattern); i++ {
		switch c := pattern[i]; {
		case c == '\\':
			i++ // skip the escaped character
		case inClass:
			if c == ']' {
				inClass = false
			}
		case c == '[':
			inClass = true
		case c == '(':
			depth++
		case c == ')':
			depth--
			if depth == 0 {
				if strings.Trim(pattern[i+1:], ")") != "" {
					return pattern, false
				}
				return pattern[:start] + "(?P<" + name + ">" + pattern[start+3:], true
			}
		}
	}
	return pattern, false
}

// escaped reports whether pattern[i] is preceded by an odd run of backslashes.
func escaped(pattern string, i int) bool {
	n := 0
	for j := i - 1; j >= 0 && pattern[j] == '\\'; j-- {
		n++
	}
	return n%2 == 1
}

// NewStreamRedactor combines rules into a single regexp. Rules whose pattern does
// not compile under RE2 are skipped (Python re.error parity). An empty or
// all-invalid rule set yields an identity redactor.
func NewStreamRedactor(rules []RedactionRule) *StreamRedactor {
	r := &StreamRedactor{}
	if len(rules) == 0 {
		return r
	}
	patterns := make([]string, 0, len(rules))
	lookaheadNames := make([]string, 0, len(rules))
	currentIndex := 1
	for i, rule := range rules {
		pattern := rule.Pattern
		name := "redact_lookahead_" + strconv.Itoa(i)
		if rewritten, ok := tailLookahead(pattern, name); ok {
			pattern = rewritten
		} else {
			name = ""
		}
		compiled, err := regexp.Compile(pattern)
		if err != nil {
			// Skip invalid / RE2-incompatible patterns (Python: except re.error).
			continue
		}
		patterns = append(patterns, "("+pattern+")")
		lookaheadNames = append(lookaheadNames, name)
		r.ruleStartIndices = append(r.ruleStartIndices, currentIndex)
		repl := rule.Replacement
		if repl == "" {
			repl = defaultReplacement
		}
		r.replacements = append(r.replacements, repl)
		currentIndex += 1 + compiled.NumSubexp()
	}
	if len(patterns) == 0 {
		return r
	}
	// The individual sub-patterns already compiled, so the join compiles too;
	// a defensive failure yields an identity redactor rather than a panic.
	combined, err := regexp.Compile(strings.Join(patterns, "|"))
	if err != nil { //nolint:wsl // defensive: unreachable once sub-patterns compiled
		return &StreamRedactor{}
	}
	r.pattern = combined
	for i, name := range lookaheadNames {
		if name == "" {
			continue
		}
		if r.lookaheadGroups == nil {
			r.lookaheadGroups = make([]int, len(lookaheadNames))
		}
		r.lookaheadGroups[i] = combined.SubexpIndex(name)
	}
	if allEqual(r.replacements) {
		single := r.replacements[0]
		r.singleReplacement = &single
	}
	return r
}

// Redact applies all rules to data in a single pass, returning the redacted
// string. An identity redactor (no compiled rules) returns data unchanged.
func (r *StreamRedactor) Redact(data string) string {
	if r.pattern == nil {
		return data
	}
	if r.lookaheadGroups != nil {
		return r.redactWithLookahead(data)
	}
	if r.singleReplacement != nil {
		// Fast path: every rule shares one replacement. ReplaceAllStringFunc
		// returns the replacement literally (no $-expansion), matching Python's
		// pattern.sub(lambda _m: single, data).
		single := *r.singleReplacement
		return r.pattern.ReplaceAllStringFunc(data, func(string) string { return single })
	}
	matches := r.pattern.FindAllStringSubmatchIndex(data, -1)
	if matches == nil {
		return data
	}
	var b strings.Builder
	last := 0
	for _, m := range matches {
		b.WriteString(data[last:m[0]])
		b.WriteString(r.replacementForMatch(m))
		last = m[1]
	}
	b.WriteString(data[last:])
	return b.String()
}

// redactWithLookahead is Redact for a rule set with a tail lookahead: match
// by match, so the lookahead's text can be left in place and searched again.
func (r *StreamRedactor) redactWithLookahead(data string) string {
	var b strings.Builder
	copied, pos := 0, 0
	for pos <= len(data) {
		m := r.pattern.FindStringSubmatchIndex(data[pos:])
		if m == nil {
			break
		}
		for k := range m {
			if m[k] >= 0 {
				m[k] += pos
			}
		}
		rule := r.ruleForMatch(m)
		end := m[1]
		if g := r.lookaheadGroups[rule]; g > 0 && m[2*g] >= 0 {
			end = m[2*g]
		}
		b.WriteString(data[copied:m[0]])
		b.WriteString(r.replacements[rule])
		copied = end
		pos = end
		if end == m[0] {
			// Nothing was replaced: step past one character so the search
			// moves on, as re.sub does after an empty match.
			if end == len(data) {
				break
			}
			_, size := utf8.DecodeRuneInString(data[end:])
			pos = end + size
		}
	}
	b.WriteString(data[copied:])
	return b.String()
}

// replacementForMatch picks the replacement for the rule that matched by
// replicating Python's re.Match.lastindex: the highest-numbered capturing group
// that participated in the match. A bisect_right over the per-rule start-group
// indices maps that group back to its owning rule. Go's regexp exposes no
// lastindex, so it is recomputed from the submatch group ranges (m[2k] < 0 means
// group k did not participate).
//
// The combined pattern guarantees the matched rule's own top-level group (index
// >= ruleStartIndices[0] == 1) participated, so last >= 1 and idx >= 0 always.
func (r *StreamRedactor) replacementForMatch(m []int) string {
	return r.replacements[r.ruleForMatch(m)]
}

// ruleForMatch is the index of the rule that produced match m (see
// replacementForMatch).
func (r *StreamRedactor) ruleForMatch(m []int) int {
	last := 0
	for k := 1; 2*k+1 < len(m); k++ {
		if m[2*k] >= 0 {
			last = k
		}
	}
	return bisectRight(r.ruleStartIndices, last) - 1
}

// bisectRight returns the insertion point for x in the sorted slice a to keep it
// sorted, with x inserted to the right of equal entries. Port of
// bisect.bisect_right.
func bisectRight(a []int, x int) int {
	lo, hi := 0, len(a)
	for lo < hi {
		mid := (lo + hi) / 2 //nolint:mnd // standard binary-search midpoint
		if x < a[mid] {
			hi = mid
		} else {
			lo = mid + 1
		}
	}
	return lo
}

// allEqual reports whether every string in s is identical (mirrors Python's
// len(set(replacements)) == 1). An empty slice is vacuously equal, but callers
// only pass non-empty slices.
func allEqual(s []string) bool {
	for i := 1; i < len(s); i++ {
		if s[i] != s[0] {
			return false
		}
	}
	return true
}
