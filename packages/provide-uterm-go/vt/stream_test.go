//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

import (
	"strings"
	"testing"
)

func TestStreamCSIParamParsing(t *testing.T) {
	// Parameters cap at 9999.
	s := feedNew(t, "\x1b[99999G")
	wantCursor(t, s, 79, 0)

	// Very long digit strings saturate without overflow.
	s = feedNew(t, "\x1b[123456789012345678901234567890G")
	wantCursor(t, s, 79, 0)

	// Leading zeros are fine; empty params default to 0.
	s = feedNew(t, "\x1b[00005GX")
	wantCursor(t, s, 5, 0)
	s = feedNew(t, "\x1b[;5H")
	wantCursor(t, s, 4, 0)
}

func TestStreamCSIAborts(t *testing.T) {
	// CAN and SUB abort the sequence (drawing the unprintable aborting
	// character is a noop); the would-be final byte becomes plain text.
	s := feedNew(t, "\x1b[31\x18mA")
	if got := s.At(0, 0); got.FG != "default" || got.Data != "m" {
		t.Errorf("after CAN abort: %+v", got)
	}
	if got := s.At(0, 1).Data; got != "A" {
		t.Errorf("after CAN abort cell 1: %q", got)
	}
	s = feedNew(t, "\x1b[31\x1amA")
	if got := s.At(0, 0); got.FG != "default" || got.Data != "m" {
		t.Errorf("after SUB abort: %+v", got)
	}
}

func TestStreamCSIControlCharactersInside(t *testing.T) {
	// BS is executed mid-sequence.
	s := feedNew(t, "ab\x1b[\x08X")
	wantCursor(t, s, 1, 0)
	// LF too.
	s = feedNew(t, "ab\x1b[\n5G")
	wantCursor(t, s, 4, 1)
}

func TestStreamCSIIgnoredCharacters(t *testing.T) {
	// SP and > are swallowed; the sequence still finishes.
	s := feedNew(t, "\x1b[ 5G")
	wantCursor(t, s, 4, 0)
	// $ skips exactly one following character.
	s = feedNew(t, "\x1b[1$qX")
	if got := s.At(0, 0).Data; got != "X" {
		t.Errorf("after $ skip: %q", got)
	}
	// Unknown finals are ignored.
	s = feedNew(t, "\x1b[5zX")
	if got := s.At(0, 0).Data; got != "X" {
		t.Errorf("after unknown final: %q", got)
	}
}

func TestStreamC1Entries(t *testing.T) {
	// Note: the C1 controls are the code points U+009B/U+009D, so the Go
	// string literals must use \u — "\x9b" would be an invalid UTF-8 byte.
	s := feedNew(t, "\u009b31mX")
	if got := s.At(0, 0); got.Data != "X" || got.FG != "red" {
		t.Errorf("CSI C1 entry: %+v", got)
	}
	s = feedNew(t, "\u009d2;via c1\x07")
	if got := s.Title(); got != "via c1" {
		t.Errorf("OSC C1 title = %q", got)
	}
}

func TestStreamOSC(t *testing.T) {
	for _, tc := range []struct {
		in          string
		title, icon string
	}{
		{"\x1b]2;term title\x07", "term title", ""},
		{"\x1b]2;st term\x1b\\", "st term", ""},
		{"\x1b]2;c1 st\u009c", "c1 st", ""},
		{"\x1b]1;icon\x07", "", "icon"},
		{"\x1b]0;both\x07", "both", "both"},
		{"\x1b]9;other\x07", "", ""},
		// ESC followed by a non-terminator is collected verbatim.
		{"\x1b]2;a\x1bqb\x07", "a\x1bqb", ""},
		// The first parameter code point is dropped unconditionally.
		{"\x1b]2title\x07", "itle", ""},
		{"\x1b]2;\x07", "", ""},
		{"\x1b]2\x07", "", ""}, // Entirely empty parameter.
	} {
		s := feedNew(t, tc.in)
		if s.Title() != tc.title || s.IconName() != tc.icon {
			t.Errorf("feed %q: title=%q icon=%q, want %q/%q",
				tc.in, s.Title(), s.IconName(), tc.title, tc.icon)
		}
	}

	// OSC R / P (palette) are swallowed without state.
	s := feedNew(t, "\x1b]R\x1b]PxyzABC")
	// "P" consumes nothing further in pyte: code P returns to ground.
	if !strings.HasPrefix(s.Display()[0], "xyzABC") {
		t.Errorf("after palette OSC: %q", s.Display()[0])
	}
}

func TestStreamEscapeDispatch(t *testing.T) {
	// NEL indexes without a carriage return (LNM is reset by default).
	s := feedNew(t, "abc\x1bEq")
	wantCursor(t, s, 4, 1)

	s = feedNew(t, "\x1b[31mjunk\x1bcX") // RIS resets everything.
	if got := s.At(0, 0); got.Data != "X" || got.FG != "default" {
		t.Errorf("after RIS: %+v", got)
	}

	// Unknown escapes are ignored.
	s = feedNew(t, "a\x1bZb")
	wantLine(t, s, 0, "ab"+strings.Repeat(" ", 78))

	// ESC % (select other charset) is a noop.
	s = feedNew(t, "a\x1b%Gb")
	wantLine(t, s, 0, "ab"+strings.Repeat(" ", 78))

	// ESC # with an unknown code is ignored.
	s = feedNew(t, "\x1b#3X")
	if got := s.At(0, 0).Data; got != "X" {
		t.Errorf("after unknown sharp: %q", got)
	}
}

func TestStreamNULAndDELIgnored(t *testing.T) {
	s := feedNew(t, "A\x00B\x7fC")
	wantLine(t, s, 0, "ABC"+strings.Repeat(" ", 77))
}

func TestStreamStatePersistsAcrossFeeds(t *testing.T) {
	s := NewScreen(80, 24)
	st := NewStream(s)
	st.Feed("\x1b")
	st.Feed("[3")
	st.Feed("1mX")
	if got := s.At(0, 0); got.Data != "X" || got.FG != "red" {
		t.Errorf("split feed: %+v", got)
	}

	st.Feed("\x1b]2;sp")
	st.Feed("lit\x07")
	if got := s.Title(); got != "split" {
		t.Errorf("split OSC title = %q", got)
	}
}

func TestStreamShiftsIgnoredInUTF8Mode(t *testing.T) {
	s := feedNew(t, "a\x0eb\x0fc")
	wantLine(t, s, 0, "abc"+strings.Repeat(" ", 77))
}

func TestStreamGroundDrawFallback(t *testing.T) {
	// Feed never routes plain text through the state machine (the fast
	// path draws it), but the ground state keeps pyte's defensive draw
	// branch; pin its behavior white-box.
	s := NewScreen(80, 24)
	st := NewStream(s)
	if !st.send('a') {
		t.Error("send('a') must stay at ground")
	}
	if got := s.At(0, 0).Data; got != "a" {
		t.Errorf("ground draw fallback: %q", got)
	}
}

func TestStreamCSIUnknownWithNULFinal(t *testing.T) {
	// NUL terminates a CSI as an unknown final (dispatching debug).
	s := feedNew(t, "ab\x1b[5\x00X")
	wantLine(t, s, 0, "abX"+strings.Repeat(" ", 77))
	// So does a stray ESC.
	s = feedNew(t, "ab\x1b[5\x1bX")
	wantLine(t, s, 0, "abX"+strings.Repeat(" ", 77))
}
