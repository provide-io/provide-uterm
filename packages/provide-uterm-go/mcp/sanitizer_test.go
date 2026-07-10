//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"strings"
	"testing"
)

func TestUnescapeKeys(t *testing.T) {
	cases := map[string]string{
		`a\nb`:       "a\nb",
		`\r\t\e`:     "\r\t\x1b",
		`\x41\x42`:   "AB",
		`A`:          "A",
		`\\n`:        `\n`,   // escaped backslash then literal n
		`\q`:         `\q`,   // unknown escape kept literal
		`\xZZ`:       `\xZZ`, // not hex -> literal
		`plain`:      "plain",
		`trailing\\`: `trailing\`,
		`\0`:         "\x00",
	}
	for in, want := range cases {
		if got := unescapeKeys(in); got != want {
			t.Errorf("unescapeKeys(%q) = %q, want %q", in, got, want)
		}
	}
	// A lone trailing backslash cannot match the alternation and is kept.
	if got := unescapeKeys(`abc\`); got != `abc\` {
		t.Errorf("trailing backslash: got %q", got)
	}
}

func TestSanitizeKeystrokes(t *testing.T) {
	// Control chars outside the allow-set are dropped; \x03 and \x1b are kept.
	if got := sanitizeKeystrokes("a\x07b\x03\x1b", 4096); got != "ab\x03\x1b" {
		t.Errorf("sanitize filtered wrong: %q", got)
	}
	// Byte cap truncates.
	long := strings.Repeat("x", 100)
	if got := sanitizeKeystrokes(long, 10); got != strings.Repeat("x", 10) {
		t.Errorf("cap wrong: %q", got)
	}
}

func TestPrepareKeystrokes(t *testing.T) {
	if got := prepareKeystrokes(`echo hi\n`, MaxKeystrokeBytes); got != "echo hi\n" {
		t.Errorf("prepareKeystrokes = %q", got)
	}
	// Unescape may introduce a disallowed byte (\x07) which sanitize then drops.
	if got := prepareKeystrokes(`a\x07b`, MaxKeystrokeBytes); got != "ab" {
		t.Errorf("prepare should drop bell: %q", got)
	}
}
