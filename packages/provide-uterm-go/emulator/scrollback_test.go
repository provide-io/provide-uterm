//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package emulator

import (
	"reflect"
	"strings"
	"testing"
)

// scrolled builds a UTF-8 emulator that records each row scrolling off its
// top, as the reference's set_scrollback_sink tests do.
func scrolled(cols, rows int) (*TerminalEmulator, *[]string) {
	e := NewWithEncoding(cols, rows, "", UTF8)
	var got []string
	e.SetScrollbackSink(func(line string) { got = append(got, line) })
	return e, &got
}

func expectRows(t *testing.T, got *[]string, want ...string) {
	t.Helper()
	if len(want) == 0 {
		want = nil
	}
	if !reflect.DeepEqual(*got, want) {
		t.Fatalf("scrolled off = %q, want %q", *got, want)
	}
}

func TestRowsScrolledOffTheTopAreHandedOverWithoutTrailingBlanks(t *testing.T) {
	e, got := scrolled(10, 3)
	e.Process([]byte("one  \r\ntwo\r\nthree\r\nfour\r\nfive"))
	expectRows(t, got, "one", "two")
	if screen := e.GetSnapshot().Screen; screen != "three     \nfour      \nfive      " {
		t.Fatalf("screen = %q", screen)
	}
}

func TestALineFeedAboveTheBottomRowScrollsNothing(t *testing.T) {
	e, got := scrolled(10, 3)
	e.Process([]byte("one\r\ntwo\r\n"))
	expectRows(t, got)
}

func TestAutowrapAtTheBottomRowScrollsTheTopRowOff(t *testing.T) {
	e, got := scrolled(4, 2)
	e.Process([]byte("top\r\nabcdef"))
	expectRows(t, got, "top")
}

func TestTheAlternateScreenHandsNothingOver(t *testing.T) {
	for _, mode := range []string{"47", "1047", "1049"} {
		e, got := scrolled(10, 3)
		e.Process([]byte("\x1b[?" + mode + "ha\r\nb\r\nc\r\nd\r\ne"))
		if len(*got) != 0 {
			t.Fatalf("mode %s: scrolled off = %q", mode, *got)
		}
	}
}

func TestARegionThatStartsAtTheTopHandsItsTopRowOver(t *testing.T) {
	e, got := scrolled(10, 4)
	e.Process([]byte("\x1b[1;3rA\r\nB\r\nC\r\nD"))
	expectRows(t, got, "A")
}

func TestARegionLowerDownHandsNothingOver(t *testing.T) {
	e, got := scrolled(10, 4)
	e.Process([]byte("title\x1b[2;4r\x1b[2;1HA\r\nB\r\nC\r\nD"))
	expectRows(t, got)
	if first := strings.Split(e.GetSnapshot().Screen, "\n")[0]; first != "title     " {
		t.Fatalf("top row = %q", first)
	}
}

func TestAWideCharacterRowIsHandedOverAsItDisplays(t *testing.T) {
	e, got := scrolled(6, 1)
	e.Process([]byte("中文\r\n"))
	expectRows(t, got, "中文")
}

func TestAResetKeepsTheSinkAndNilStopsIt(t *testing.T) {
	e, got := scrolled(10, 2)
	e.Reset()
	e.Process([]byte("a\r\nb\r\n"))
	expectRows(t, got, "a")
	e.SetScrollbackSink(nil)
	e.Process([]byte("c\r\n"))
	expectRows(t, got, "a")
}
