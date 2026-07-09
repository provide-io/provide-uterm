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

func TestMoveToAndClearScreen(t *testing.T) {
	if got := MoveTo(3, 7); got != "\x1b[3;7H" {
		t.Fatalf("got %q", got)
	}
	if got := ClearScreen(); got != "\x1b[2J" {
		t.Fatalf("got %q", got)
	}
}

func TestStyleToSGR(t *testing.T) {
	cases := []struct {
		style Style
		want  string
	}{
		{DefaultStyle, ANSIReset},
		{Style{FG: "red", BG: "default"}, "\x1b[31m"},
		{Style{FG: "default", BG: "blue"}, "\x1b[44m"},
		{Style{FG: "brightcyan", BG: "black", Bold: true}, "\x1b[1;96;40m"},
		{Style{FG: "default", BG: "default", Underscore: true, Blink: true}, "\x1b[4;5m"},
		// Reverse swaps fg/bg before mapping.
		{Style{FG: "red", BG: "blue", Reverse: true}, "\x1b[34;41m"},
		// pyte hex colors become truecolor operands.
		{Style{FG: "ff8000", BG: "default"}, "\x1b[38;2;255;128;0m"},
		{Style{FG: "default", BG: "0000FF"}, "\x1b[48;2;0;0;255m"},
		// Unknown color names emit nothing.
		{Style{FG: "chartreuse-ish", BG: "default"}, ANSIReset},
		{Style{FG: "zzzzzz", BG: "default"}, ANSIReset}, // 6 chars but not hex
	}
	for _, c := range cases {
		if got := StyleToSGR(c.style); got != c.want {
			t.Fatalf("StyleToSGR(%+v) = %q want %q", c.style, got, c.want)
		}
	}
}

func TestAnsiBufferFeedAndRenderLines(t *testing.T) {
	b := NewAnsiBuffer(10, 3)
	b.Feed([]byte("\x1b[31mAB\x1b[0mC"))
	b.Feed(nil) // no-op
	lines := b.RenderLines(10, 3)
	if len(lines) != 3 {
		t.Fatalf("lines = %d", len(lines))
	}
	if !strings.HasPrefix(lines[0], "\x1b[31mAB\x1b[0mC") {
		t.Fatalf("row0 = %q", lines[0])
	}
	if !strings.HasSuffix(lines[0], ANSIReset) {
		t.Fatalf("row0 missing trailing reset: %q", lines[0])
	}
	// CP437 high bytes decode to box drawing.
	b2 := NewAnsiBuffer(10, 2)
	b2.Feed([]byte{0xC9, 0xCD})
	if !strings.Contains(b2.RenderLines(10, 2)[0], "╔═") {
		t.Fatalf("row0 = %q", b2.RenderLines(10, 2)[0])
	}
	if b2.Screen() == nil {
		t.Fatal("nil screen")
	}
}

func TestAnsiBufferResetAndResize(t *testing.T) {
	b := NewAnsiBuffer(10, 2)
	b.Feed([]byte("xyz"))
	b.Reset()
	row := b.RenderLines(10, 1)[0]
	if strings.Contains(row, "xyz") {
		t.Fatalf("row = %q", row)
	}
	b.Resize(5, 1)
	lines := b.RenderLines(5, 1)
	if len(lines) != 1 {
		t.Fatalf("lines = %v", lines)
	}
}

func TestRenderLinesStyleRuns(t *testing.T) {
	b := NewAnsiBuffer(6, 1)
	b.Feed([]byte("\x1b[1;33mAA\x1b[0mBB"))
	row := b.RenderLines(6, 1)[0]
	// pyte names SGR 33 "brown", which is not in FG_CODES — like the Python
	// renderer, only the bold attribute survives, emitted once per run.
	if !strings.HasPrefix(row, "\x1b[1mAA\x1b[0mBB") {
		t.Fatalf("row = %q", row)
	}
	if !strings.HasSuffix(row, ANSIReset) {
		t.Fatalf("row = %q", row)
	}
}

func TestCellStyleEmptyColorsDefault(t *testing.T) {
	got := CellStyle(vt.Char{Data: "x"})
	if got.FG != "default" || got.BG != "default" {
		t.Fatalf("got %+v", got)
	}
}

func TestRenderScreenLinesWideCharStub(t *testing.T) {
	s := vt.NewScreen(4, 1)
	vt.NewStream(s).Feed("你")
	row := RenderScreenLines(s, 4, 1)[0]
	// The wide char occupies two cells; its stub renders as a space.
	if !strings.Contains(row, "你") {
		t.Fatalf("row = %q", row)
	}
}
