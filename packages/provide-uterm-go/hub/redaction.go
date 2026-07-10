//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"regexp"
	"strings"
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
// RE2 caveat: patterns are Go regexps. A pattern using a Python-`re`-only
// construct that RE2 rejects (lookahead/lookbehind/backreferences — e.g. the
// default generic password/api_key/token rules use `(?=...)` lookahead) fails
// to compile and is SKIPPED, exactly like the Python invalid-regex path
// (`except re.error: continue`). Skipped rules simply do not redact; the
// remaining rules are unaffected.
type StreamRedactor struct {
	pattern           *regexp.Regexp
	ruleStartIndices  []int
	replacements      []string
	singleReplacement *string
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
	currentIndex := 1
	for _, rule := range rules {
		compiled, err := regexp.Compile(rule.Pattern)
		if err != nil {
			// Skip invalid / RE2-incompatible patterns (Python: except re.error).
			continue
		}
		patterns = append(patterns, "("+rule.Pattern+")")
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
	last := 0
	for k := 1; 2*k+1 < len(m); k++ {
		if m[2*k] >= 0 {
			last = k
		}
	}
	idx := bisectRight(r.ruleStartIndices, last) - 1
	return r.replacements[idx]
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
