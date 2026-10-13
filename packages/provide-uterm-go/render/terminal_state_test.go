//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package render

import (
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/vt"
)

// The snapshot-resume tests in the emulator package drive these through a
// CP437 emulator; the cases here need a stream that honours charsets, or a
// character only UTF-8 can carry.

func charsetScreen(cols, rows int, text string) *vt.Screen {
	s := vt.NewScreen(cols, rows)
	stream := vt.NewStream(s)
	stream.UseUTF8 = false
	stream.Feed(text)
	return s
}

func TestScreenStateSuffixRestoresCharsets(t *testing.T) {
	if got := ScreenStateSuffix(charsetScreen(20, 3, "\x1b)0\x0eqqq\x1b(0x"), true); !strings.Contains(got, "\x1b(0\x1b)0\x0e") {
		t.Fatalf("suffix %q", got)
	}
	// The power-on sets (G0 B, G1 0, shifted in) send nothing.
	if got := ScreenStateSuffix(charsetScreen(20, 3, ""), true); strings.Contains(got, "\x1b(") || strings.Contains(got, "\x1b)") {
		t.Fatalf("suffix %q", got)
	}
	// A designation matching the power-on sets is still sent: curses under
	// TERM=screen designates G1 once and draws every line with SO/SI.
	designated := charsetScreen(20, 3, "\x1b(B\x1b)0\x0eqq\x0f")
	if got := ScreenStateSuffix(designated, true); !strings.Contains(got, "\x1b(B\x1b)0\x1b[0m") {
		t.Fatalf("suffix %q", got)
	}
	designated.Reset()
	if got := ScreenStateSuffix(designated, true); strings.Contains(got, "\x1b)") {
		t.Fatalf("suffix after reset %q", got)
	}
	if got := ScreenStateSuffix(charsetScreen(20, 3, "\x1b)B"), true); !strings.Contains(got, "\x1b(B\x1b)B") {
		t.Fatalf("suffix %q", got)
	}
	for _, code := range []string{"B", "0", "U", "V"} {
		got := ScreenStateSuffix(charsetScreen(20, 3, "\x1b("+code+"\x1b)"+code), true)
		if !strings.Contains(got, "\x1b("+code+"\x1b)"+code) {
			t.Fatalf("code %s: suffix %q", code, got)
		}
	}
	// A stream that ignores designations has none to restore.
	if got := ScreenStateSuffix(charsetScreen(20, 3, "\x1b)B\x0e"), false); strings.Contains(got, "\x1b)") {
		t.Fatalf("suffix %q", got)
	}
}

func TestScreenStateSuffixRedrawsAWideCharacterForTheDeferredWrap(t *testing.T) {
	s := charsetScreen(20, 3, "\x1b[1;19H中")
	if c := s.Cursor(); c.X != 20 || c.Y != 0 {
		t.Fatalf("cursor %+v", c)
	}
	if got := ScreenStateSuffix(s, true); !strings.Contains(got, "\x1b[1;19H\x1b[0m中") {
		t.Fatalf("suffix %q", got)
	}
}

func TestPenSGR(t *testing.T) {
	if got := PenSGR(vt.Char{}); got != "\x1b[0m" {
		t.Fatalf("got %q", got)
	}
	if got := PenSGR(vt.Char{FG: "brown", BG: "brightbrown", Italics: true, Strikethrough: true}); got != "\x1b[0;3;9;33;103m" {
		t.Fatalf("got %q", got)
	}
}

func TestScreenStatePrefixEntersTheAlternateScreen(t *testing.T) {
	if got := ScreenStatePrefix(charsetScreen(20, 3, "")); got != "" {
		t.Fatalf("prefix %q", got)
	}
	if got := ScreenStatePrefix(charsetScreen(20, 3, "\x1b[?47h\x1b[?1049h")); got != "\x1b[?47h\x1b[?1049h\x1b[H" {
		t.Fatalf("prefix %q", got)
	}
}

func TestScreenStateSuffixCases(t *testing.T) {
	fresh := "\x1b[r\x1b[?6l\x1b[?7h\x1b[4l\x1b[1;1H\x1b[20l\x1b[?1l\x1b[0m\x1b[?25h"
	cases := []struct {
		name, stream, want string
	}{
		{"a fresh screen", "", fresh},
		{"a whole-screen region is none", "\x1b[1;5r", fresh},
		{"region and origin", "\x1b[2;4r\x1b[?6h\x1b[2;3H", "\x1b[2;4r\x1b[?6h\x1b[?7h\x1b[4l\x1b[2;3H\x1b[20l\x1b[?1l\x1b[0m\x1b[?25h"},
		{"origin without a region", "\x1b[?6h\x1b[2;3H", "\x1b[r\x1b[?6h\x1b[?7h\x1b[4l\x1b[2;3H\x1b[20l\x1b[?1l\x1b[0m\x1b[?25h"},
		{"region without origin", "\x1b[2;4r\x1b[3;3H", "\x1b[2;4r\x1b[?6l\x1b[?7h\x1b[4l\x1b[3;3H\x1b[20l\x1b[?1l\x1b[0m\x1b[?25h"},
		{"deferred wrap", "\x1b[1;9H\x1b[31mab", "\x1b[r\x1b[?6l\x1b[?7h\x1b[4l\x1b[1;10H\x1b[0;31mb\x1b[20l\x1b[?1l\x1b[0;31m\x1b[?25h"},
		{"modes", "\x1b[?7l\x1b[4h\x1b[20h\x1b[?1h\x1b[?1005h\x1b[?1015h\x1b[?25l", "\x1b[r\x1b[?6l\x1b[?7h\x1b[4l\x1b[1;1H\x1b[?7l\x1b[4h\x1b[20h\x1b[?1h\x1b[?1005h\x1b[?1015h\x1b[0m\x1b[?25l"},
	}
	for _, c := range cases {
		if got := ScreenStateSuffix(charsetScreen(10, 5, c.stream), false); got != c.want {
			t.Fatalf("%s: suffix %q\n want %q", c.name, got, c.want)
		}
	}
}
