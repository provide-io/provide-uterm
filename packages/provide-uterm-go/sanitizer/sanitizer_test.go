//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sanitizer

import (
	"strings"
	"testing"
)

func TestUnescapeKeys(t *testing.T) {
	cases := []struct{ in, want string }{
		{`hello\r`, "hello\r"},
		{`\n\r\t\e\0`, "\n\r\t\x1b\x00"},
		{`\\`, `\`},
		{`\'\"`, `'"`},
		{`\x1b[A`, "\x1b[A"},
		{`\x41`, "A"},
		{"\\u2192", "→"}, // 4-digit unicode escape
		{`A`, "A"},
		{`→`, "→"},
		{`\q`, `\q`},     // unknown escape preserved
		{"\\\n", "\\\n"}, // DOTALL: backslash-newline preserved
		{`plain`, "plain"},
		{`trailing\`, `trailing\`}, // lone trailing backslash preserved
		{`\xZZ`, `\xZZ`},           // bad hex: (.) matches "x", \x preserved
	}
	for _, c := range cases {
		if got := UnescapeKeys(c.in); got != c.want {
			t.Fatalf("UnescapeKeys(%q) = %q want %q", c.in, got, c.want)
		}
	}
}

func TestUnescapeKeysBadHexMatchesSingleChar(t *testing.T) {
	// `\xZZ`: the x2-hex alternative fails, `(.)` matches "x" which is not a
	// simple escape, so `\x` is preserved and "ZZ" passes through — same as
	// Python.
	if got := UnescapeKeys(`\x1`); got != `\x1` {
		t.Fatalf("got %q", got)
	}
}

func TestSanitizeKeystrokes(t *testing.T) {
	if got := SanitizeKeystrokes("abc\x03\x1b[A\r\n\t\v\f", DefaultMaxBytes); got != "abc\x03\x1b[A\r\n\t\v\f" {
		t.Fatalf("got %q", got)
	}
	// Non-printable control and non-ASCII filtered out.
	if got := SanitizeKeystrokes("a\x00b\x7fc→d\x02", DefaultMaxBytes); got != "abcd" {
		t.Fatalf("got %q", got)
	}
	// Printable-ASCII boundary: space (0x20) and tilde (0x7E) are the inclusive
	// edges of the allowed range and must survive filtering.
	if got := SanitizeKeystrokes(" ~", DefaultMaxBytes); got != " ~" {
		t.Fatalf("boundary got %q", got)
	}
}

func TestSanitizeKeystrokesTruncates(t *testing.T) {
	long := strings.Repeat("x", 5000)
	got := SanitizeKeystrokes(long, 4096)
	if len(got) != 4096 {
		t.Fatalf("len = %d", len(got))
	}
	if got := SanitizeKeystrokes("abc", 2); got != "ab" {
		t.Fatalf("got %q", got)
	}
}

func TestPrepareKeystrokes(t *testing.T) {
	if got := PrepareKeystrokes(`ls -la\r`, DefaultMaxBytes); got != "ls -la\r" {
		t.Fatalf("got %q", got)
	}
	// Unescaped-then-filtered: \x00 survives unescape but is filtered.
	if got := PrepareKeystrokes(`a\0b`, DefaultMaxBytes); got != "ab" {
		t.Fatalf("got %q", got)
	}
}
