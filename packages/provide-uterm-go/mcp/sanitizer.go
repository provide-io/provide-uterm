//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"strconv"
	"strings"
)

// Keystroke unescaping and sanitization shared by the MCP hijack_send tool.
// Port of provide.uterm.sanitizer (unescape_keys / sanitize_keystrokes /
// prepare_keystrokes). The Go client does not sanitize keystrokes itself, so
// the MCP layer owns this the same way the Python tool does.

// simpleEscapes maps a backslash-escaped character to its literal replacement,
// mirroring Python _SIMPLE_ESCAPES.
var simpleEscapes = map[rune]rune{
	'n':  '\n',
	'r':  '\r',
	't':  '\t',
	'e':  '\x1b',
	'0':  '\x00',
	'\\': '\\',
	'\'': '\'',
	'"':  '"',
}

// isHex reports whether r is an ASCII hex digit.
func isHex(r rune) bool {
	return (r >= '0' && r <= '9') || (r >= 'a' && r <= 'f') || (r >= 'A' && r <= 'F')
}

// unescapeKeys translates terminal-relevant escape sequences in raw, replicating
// the Python regex r"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|(.))" (DOTALL)
// applied left-to-right, non-overlapping.
func unescapeKeys(raw string) string {
	runes := []rune(raw)
	var b strings.Builder
	i, n := 0, len(runes)
	for i < n {
		if runes[i] != '\\' {
			b.WriteRune(runes[i])
			i++
			continue
		}
		// A lone trailing backslash cannot match the alternation (which needs at
		// least one following char), so it is emitted verbatim.
		if i+1 >= n {
			b.WriteRune('\\')
			i++
			continue
		}
		next := runes[i+1]
		// \xHH — two hex digits.
		if next == 'x' && i+4 <= n && isHex(runes[i+2]) && isHex(runes[i+3]) {
			v, _ := strconv.ParseInt(string(runes[i+2:i+4]), 16, 32)
			b.WriteRune(rune(v))
			i += 4
			continue
		}
		// \uHHHH — four hex digits.
		if next == 'u' && i+6 <= n && isHex(runes[i+2]) && isHex(runes[i+3]) && isHex(runes[i+4]) && isHex(runes[i+5]) {
			v, _ := strconv.ParseInt(string(runes[i+2:i+6]), 16, 32)
			b.WriteRune(rune(v))
			i += 6
			continue
		}
		// \c — single char: mapped simple escape, else the literal "\c".
		if repl, ok := simpleEscapes[next]; ok {
			b.WriteRune(repl)
		} else {
			b.WriteRune('\\')
			b.WriteRune(next)
		}
		i += 2
	}
	return b.String()
}

// isAllowedKeystrokeRune replicates Python's allowed set:
// set(string.printable) | {"\r", "\n", "\t", "\x03", "\x1b"}.
func isAllowedKeystrokeRune(r rune) bool {
	switch r {
	case '\t', '\n', '\r', '\x0b', '\x0c', '\x03', '\x1b':
		return true
	}
	return r >= 0x20 && r <= 0x7e
}

// sanitizeKeystrokes filters non-printable bytes while preserving terminal input
// controls, then caps the result at maxBytes bytes (the reference decodes the
// truncated tail with "ignore", which on this ASCII-only output is a no-op).
func sanitizeKeystrokes(keys string, maxBytes int) string {
	var b strings.Builder
	for _, r := range keys {
		if isAllowedKeystrokeRune(r) {
			b.WriteRune(r)
		}
	}
	filtered := b.String()
	if len(filtered) <= maxBytes {
		return filtered
	}
	// isAllowedKeystrokeRune admits ASCII only, so every byte of filtered is a
	// whole rune and a cut at any offset is already what the reference's
	// bytes.decode("utf-8", "ignore") would leave: there is no split tail to drop.
	return filtered[:maxBytes]
}

// prepareKeystrokes unescapes then sanitizes keystrokes. Port of
// provide.uterm.sanitizer.prepare_keystrokes.
func prepareKeystrokes(raw string, maxBytes int) string {
	return sanitizeKeystrokes(unescapeKeys(raw), maxBytes)
}
