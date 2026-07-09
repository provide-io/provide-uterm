//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package sanitizer provides keystroke unescaping and sanitization shared by
// direct sessions and MCP. Port of provide.uterm.sanitizer.
package sanitizer

import (
	"regexp"
	"strconv"
	"strings"
)

// DefaultMaxBytes mirrors the Python max_bytes=4096 default.
const DefaultMaxBytes = 4096

var simpleEscapes = map[string]string{
	"n":  "\n",
	"r":  "\r",
	"t":  "\t",
	"e":  "\x1b",
	"0":  "\x00",
	"\\": "\\",
	"'":  "'",
	"\"": "\"",
}

// (?s) mirrors Python's re.DOTALL so `\<newline>` is matched (and preserved).
var escapePattern = regexp.MustCompile(`(?s)\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|(.))`)

// UnescapeKeys translates terminal-relevant escape sequences in raw.
func UnescapeKeys(raw string) string {
	return escapePattern.ReplaceAllStringFunc(raw, func(match string) string {
		groups := escapePattern.FindStringSubmatch(match)
		hex2, hex4, ch := groups[1], groups[2], groups[3]
		if hex2 != "" {
			v, _ := strconv.ParseUint(hex2, 16, 32)
			return string(rune(v))
		}
		if hex4 != "" {
			v, _ := strconv.ParseUint(hex4, 16, 32)
			return string(rune(v))
		}
		if repl, ok := simpleEscapes[ch]; ok {
			return repl
		}
		return match
	})
}

// allowedRune mirrors Python's `set(string.printable) | {"\r","\n","\t","\x03","\x1b"}`:
// ASCII printable 0x20-0x7E plus \t \n \r \v \f, ETX (Ctrl-C) and ESC.
func allowedRune(r rune) bool {
	if r >= 0x20 && r <= 0x7E {
		return true
	}
	switch r {
	case '\t', '\n', '\r', '\v', '\f', '\x03', '\x1b':
		return true
	}
	return false
}

// SanitizeKeystrokes filters non-printable bytes while preserving terminal
// input controls, then truncates to maxBytes. The allowed set is pure ASCII,
// so a byte-offset cut can never split a rune (Python's decode
// errors="ignore" after the byte slice is a no-op for ASCII).
func SanitizeKeystrokes(keys string, maxBytes int) string {
	var b strings.Builder
	b.Grow(len(keys))
	for _, r := range keys {
		if allowedRune(r) {
			b.WriteRune(r)
		}
	}
	filtered := b.String()
	if len(filtered) <= maxBytes {
		return filtered
	}
	return filtered[:maxBytes]
}

// PrepareKeystrokes unescapes then sanitizes keystrokes.
func PrepareKeystrokes(raw string, maxBytes int) string {
	return SanitizeKeystrokes(UnescapeKeys(raw), maxBytes)
}
