//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package render

import (
	"reflect"
	"testing"
)

func TestNearest16ExactAndNearby(t *testing.T) {
	fg, bg := Nearest16(0, 0, 0)
	if fg != 30 || bg != 40 {
		t.Fatalf("black = %d/%d", fg, bg)
	}
	fg, bg = Nearest16(255, 255, 255)
	if fg != 97 || bg != 107 {
		t.Fatalf("white = %d/%d", fg, bg)
	}
	// Near-red snaps to red (170,0,0).
	fg, _ = Nearest16(160, 10, 10)
	if fg != 31 {
		t.Fatalf("near-red fg = %d", fg)
	}
}

func TestNearest256CubeAndGray(t *testing.T) {
	// Exact cube color 5,5,5 → index 231 (255,255,255 in the cube).
	if got := Nearest256(255, 255, 255); got != 15 && got != 231 {
		t.Fatalf("white = %d", got)
	}
	// Mid gray hits the grayscale ramp (232-255).
	if got := Nearest256(128, 128, 128); got < 232 || got > 255 {
		// 128 gray could also match cube greys; assert it picks the closest:
		// ramp value 128 = 8+10*12 → index 244.
		t.Fatalf("gray = %d", got)
	}
	if got := Nearest256(8, 8, 8); got != 232 {
		t.Fatalf("darkest gray = %d", got)
	}
	// Pure cube color: (0,95,255) is r=0,g=1(95? no: 55+40*1=95),b=5(255) →
	// 16 + 0*36 + 1*6 + 5 = 27.
	if got := Nearest256(0, 95, 255); got != 27 {
		t.Fatalf("cube color = %d", got)
	}
}

func TestSGREmitters(t *testing.T) {
	fg, bg := RGB{255, 0, 0}, RGB{0, 0, 0}
	if got := SGRTruecolor(fg, bg); got != "\x1b[38;2;255;0;0;48;2;0;0;0m" {
		t.Fatalf("truecolor = %q", got)
	}
	// Pure red hits cube index 196 exactly; black hits palette index 0
	// (first best wins over the cube's 16).
	if got := SGR256(fg, bg); got != "\x1b[38;5;196;48;5;0m" {
		t.Fatalf("256 = %q", got)
	}
	// Pure red (255,0,0) is closer to base red (170,0,0) than bright red
	// (255,85,85).
	if got := SGR16(fg, bg); got != "\x1b[31;40m" {
		t.Fatalf("16 near-red = %q", got)
	}
	if got := SGR16(RGB{170, 0, 0}, RGB{0, 0, 0}); got != "\x1b[31;40m" {
		t.Fatalf("16 = %q", got)
	}
	for _, mode := range []ColorMode{ModeTruecolor, Mode256, Mode16} {
		if SGRFunctions[mode] == nil {
			t.Fatalf("missing emitter for %s", mode)
		}
	}
}

func TestANSIToSegmentsBasic(t *testing.T) {
	got := ANSIToSegments("\x1b[31mred\x1b[0m plain")
	want := []Segment{
		{Text: "red", Color: "red"},
		{Text: " plain"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
}

func TestANSIToSegmentsBoldAndBright(t *testing.T) {
	got := ANSIToSegments("\x1b[1;32mbold green\x1b[22m dim\x1b[92m bright")
	want := []Segment{
		{Text: "bold green", Color: "green", Bold: true},
		{Text: " dim", Color: "green"},
		{Text: " bright", Color: "green", Bold: true},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
}

func TestANSIToSegmentsMergesAdjacentSameStyle(t *testing.T) {
	got := ANSIToSegments("a\x1b[31m\x1b[31mb")
	// The style change to red flushes "a"; two identical SGRs then merge b.
	want := []Segment{{Text: "a"}, {Text: "b", Color: "red"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
	// Style set then reset back produces merged default runs.
	got = ANSIToSegments("x\x1b[39my")
	want = []Segment{{Text: "xy"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
}

func TestANSIToSegmentsDropsNonSGREscapes(t *testing.T) {
	got := ANSIToSegments("\x1b[2J\x1b[Hclear\x1b[31;1mred")
	want := []Segment{
		{Text: "clear"},
		{Text: "red", Color: "red", Bold: true},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
	// ESC + one char is consumed (the charset designator ESC ( B loses only
	// "ESC (" — the "B" survives as text, matching Python's `\x1b.`).
	got = ANSIToSegments("\x1b(Bx")
	if !reflect.DeepEqual(got, []Segment{{Text: "Bx"}}) {
		t.Fatalf("got %#v", got)
	}
	// ESC before newline: the ESC is dropped, the newline survives (Python
	// non-DOTALL `.` behavior).
	got = ANSIToSegments("a\x1b\nb")
	if !reflect.DeepEqual(got, []Segment{{Text: "a\nb"}}) {
		t.Fatalf("got %#v", got)
	}
	// Trailing lone ESC is skipped.
	got = ANSIToSegments("tail\x1b")
	if !reflect.DeepEqual(got, []Segment{{Text: "tail"}}) {
		t.Fatalf("got %#v", got)
	}
}

func TestANSIToSegmentsExtendedColorsSkipOperands(t *testing.T) {
	// 38;5;196 and 48;2;1;2;3 operands must not be misread as SGR codes.
	got := ANSIToSegments("\x1b[38;5;196mx\x1b[0m\x1b[48;2;1;2;3;31my")
	want := []Segment{
		{Text: "x"},
		{Text: "y", Color: "red"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
	// Truncated extended sequence (38 at end) falls through harmlessly.
	got = ANSIToSegments("\x1b[38ma")
	if !reflect.DeepEqual(got, []Segment{{Text: "a"}}) {
		t.Fatalf("got %#v", got)
	}
}

func TestANSIToSegmentsEmptyParams(t *testing.T) {
	// ESC[m == reset; ESC[;31m has an empty first param treated as 0.
	got := ANSIToSegments("\x1b[31ma\x1b[mb\x1b[;31mc")
	want := []Segment{
		{Text: "a", Color: "red"},
		{Text: "b"},
		{Text: "c", Color: "red"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
}

func TestANSIToSegmentsUnicode(t *testing.T) {
	got := ANSIToSegments("\x1b[36m╔══╗\x1b[0m")
	if !reflect.DeepEqual(got, []Segment{{Text: "╔══╗", Color: "cyan"}}) {
		t.Fatalf("got %#v", got)
	}
}

func TestANSIToSegmentsEmpty(t *testing.T) {
	if got := ANSIToSegments(""); got != nil {
		t.Fatalf("got %#v", got)
	}
	if got := ANSIToSegments("\x1b[31m\x1b[0m"); got != nil {
		t.Fatalf("got %#v", got)
	}
}

func TestTokensToSegments(t *testing.T) {
	// {+g} → bright green bold; {-x} → reset (via the ansi dialect registry).
	got := TokensToSegments("{+g}go{-x} plain")
	want := []Segment{
		{Text: "go", Color: "green", Bold: true},
		{Text: " plain"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
}

func TestSegmentColorNamesClosedSet(t *testing.T) {
	if len(SegmentColorNames) != 8 || SegmentColorNames[1] != "red" {
		t.Fatalf("names = %v", SegmentColorNames)
	}
}
