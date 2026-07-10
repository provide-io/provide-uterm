//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import "strings"

// isPyWS reports whether b is one of the ASCII bytes Python's str.split(None)
// and str.strip() treat as whitespace.
func isPyWS(b byte) bool {
	switch b {
	case ' ', '\t', '\n', '\r', '\f', '\v':
		return true
	default:
		return false
	}
}

// pySplit1 mimics Python str.split(None, 1): it strips leading whitespace,
// takes the first whitespace-delimited token, and (when more remains) returns
// the remainder with its own leading whitespace stripped and trailing
// whitespace preserved. Returns nil for empty / all-whitespace input, a
// one-element slice when there is no remainder, else two elements.
func pySplit1(s string) []string {
	start := 0
	for start < len(s) && isPyWS(s[start]) {
		start++
	}
	if start == len(s) {
		return nil
	}
	j := start
	for j < len(s) && !isPyWS(s[j]) {
		j++
	}
	head := s[start:j]
	k := j
	for k < len(s) && isPyWS(s[k]) {
		k++
	}
	if k == len(s) {
		return []string{head}
	}
	return []string{head, s[k:]}
}

// pyStrip mimics Python str.strip() for the ASCII whitespace set.
func pyStrip(s string) string {
	return strings.TrimFunc(s, func(r rune) bool {
		return r < 0x80 && isPyWS(byte(r))
	})
}

// pyFields mimics Python str.split() with no arguments: split on runs of
// whitespace, dropping empty tokens.
func pyFields(s string) []string {
	return strings.FieldsFunc(s, func(r rune) bool {
		return r < 0x80 && isPyWS(byte(r))
	})
}

// truthy mimics Python truthiness for the value types a JSON-shaped context
// map yields (nil, bool, string, numbers).
func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case int:
		return x != 0
	case int64:
		return x != 0
	case float64:
		return x != 0
	default:
		return true
	}
}
