//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package screen

import (
	"strings"
	"unicode"
)

// isPySpace reports whether r matches Python's re \s (equivalently
// str.isspace) for str patterns. That set is Go's unicode.IsSpace plus the
// information separators U+001C-U+001F (bidirectional classes B/S), which
// lack the Unicode White_Space property and are therefore missed by
// unicode.IsSpace.
func isPySpace(r rune) bool {
	return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f)
}

// isPyDigit reports whether r matches Python's re \d for str patterns:
// Unicode decimal digits (category Nd), which is exactly Go's
// unicode.IsDigit.
func isPyDigit(r rune) bool {
	return unicode.IsDigit(r)
}

// pyStrip mirrors Python's str.strip() with no arguments (trims the
// str.isspace character set from both ends).
func pyStrip(s string) string {
	return strings.TrimFunc(s, isPySpace)
}

// isPyLineTerminator reports whether r is one of the line boundaries
// recognized by Python's str.splitlines().
func isPyLineTerminator(r rune) bool {
	switch r {
	case '\n', '\r', '\v', '\f', 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029:
		return true
	}
	return false
}

// pySplitLines mirrors Python's str.splitlines(): it splits on the full set
// of Python line boundaries (treating "\r\n" as a single boundary), drops
// the terminators, and yields no trailing empty line. An empty string
// yields an empty slice.
func pySplitLines(s string) []string {
	rs := []rune(s)
	lines := []string{}
	start := 0
	for i := 0; i < len(rs); {
		if !isPyLineTerminator(rs[i]) {
			i++
			continue
		}
		lines = append(lines, string(rs[start:i]))
		if rs[i] == '\r' && i+1 < len(rs) && rs[i+1] == '\n' {
			i++
		}
		i++
		start = i
	}
	if start < len(rs) {
		lines = append(lines, string(rs[start:]))
	}
	return lines
}
