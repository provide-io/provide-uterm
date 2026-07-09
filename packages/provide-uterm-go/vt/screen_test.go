//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

import (
	"reflect"
	"strings"
	"testing"
)

// feedNew creates a fresh 80x24 screen+stream, feeds data, and returns the
// screen.
func feedNew(t *testing.T, data string) *Screen {
	t.Helper()
	s := NewScreen(80, 24)
	NewStream(s).Feed(data)
	return s
}

// feedSized is feedNew with explicit dimensions.
func feedSized(t *testing.T, cols, rows int, data string) *Screen {
	t.Helper()
	s := NewScreen(cols, rows)
	NewStream(s).Feed(data)
	return s
}

func wantLine(t *testing.T, s *Screen, y int, want string) {
	t.Helper()
	got := s.Display()[y]
	if got != want {
		t.Errorf("line %d = %q, want %q", y, got, want)
	}
}

func wantCursor(t *testing.T, s *Screen, x, y int) {
	t.Helper()
	c := s.Cursor()
	if c.X != x || c.Y != y {
		t.Errorf("cursor = (%d, %d), want (%d, %d)", c.X, c.Y, x, y)
	}
}

func TestDrawAndAutowrap(t *testing.T) {
	s := feedNew(t, strings.Repeat("x", 80))
	wantCursor(t, s, 80, 0) // Phantom column: past the last cell.
	wantLine(t, s, 0, strings.Repeat("x", 80))

	NewStream(s).Feed("y")
	wantCursor(t, s, 1, 1)
	wantLine(t, s, 1, "y"+strings.Repeat(" ", 79))
}

func TestDrawNoWrapOverstrikesLastColumn(t *testing.T) {
	s := feedNew(t, "\x1b[?7l"+strings.Repeat("x", 80)+"AB")
	wantLine(t, s, 0, strings.Repeat("x", 79)+"B")
	wantCursor(t, s, 80, 0)
	wantLine(t, s, 1, strings.Repeat(" ", 80))
}

func TestDrawUnprintableAbortsBatch(t *testing.T) {
	// \x01 is not special to the stream, so "AB\x01CD" is one Draw batch
	// that pyte aborts at the unprintable character.
	s := feedNew(t, "AB\x01CD")
	wantLine(t, s, 0, "AB"+strings.Repeat(" ", 78))
}

func TestEraseInLineModes(t *testing.T) {
	for _, tc := range []struct {
		how  int
		want string
	}{
		{0, "AB" + strings.Repeat(" ", 78)},
		{1, "   DEF" + strings.Repeat(" ", 74)},
		{2, strings.Repeat(" ", 80)},
	} {
		s := feedNew(t, "ABCDEF\x1b[1;3H")
		s.EraseInLine(tc.how, false)
		wantLine(t, s, 0, tc.want)
	}
	// Unknown how is a noop (pyte would throw here).
	s := feedNew(t, "ABCDEF\x1b[1;3H")
	s.EraseInLine(7, false)
	wantLine(t, s, 0, "ABCDEF"+strings.Repeat(" ", 74))
}

func TestEraseInDisplayModes(t *testing.T) {
	setup := "A\r\nB\r\nC\r\nD\x1b[2;1H"
	for _, tc := range []struct {
		how  int
		want []string
	}{
		{0, []string{"A", "", "", ""}},
		{1, []string{"", "", "C", "D"}},
		{2, []string{"", "", "", ""}},
		{3, []string{"", "", "", ""}},
	} {
		s := feedNew(t, setup+"")
		s.EraseInDisplay(tc.how)
		for y, w := range tc.want {
			wantLine(t, s, y, w+strings.Repeat(" ", 80-len(w)))
		}
	}
	s := feedNew(t, setup)
	s.EraseInDisplay(9) // Unknown how: noop.
	wantLine(t, s, 0, "A"+strings.Repeat(" ", 79))
}

func TestEraseUsesCursorAttrsOnStoredCellsOnly(t *testing.T) {
	// pyte quirk: ED rewrites only explicitly stored cells with the
	// cursor attributes; untouched cells keep default rendering.
	s := feedNew(t, "AB\x1b[41m\x1b[2J")
	if got := s.At(0, 0).BG; got != "red" {
		t.Errorf("stored cell BG = %q, want red", got)
	}
	if got := s.At(0, 5).BG; got != "default" {
		t.Errorf("unstored cell BG = %q, want default", got)
	}
	if got := s.At(1, 0).BG; got != "default" {
		t.Errorf("untouched row BG = %q, want default", got)
	}
}

func TestMarginsAndIndexScrolling(t *testing.T) {
	// DECSTBM sets 0-based margins and homes the cursor.
	s := feedNew(t, "\x1b[5;15r")
	m, ok := s.Margins()
	if !ok || m.Top != 4 || m.Bottom != 14 {
		t.Fatalf("margins = %+v ok=%v, want {4 14} true", m, ok)
	}
	wantCursor(t, s, 0, 0)

	// IND at the bottom margin scrolls only the region.
	s = feedNew(t, "\x1b[2;3r\x1b[1;1HL0\x1b[2;1HL1\x1b[3;1HL2\x1b[4;1HL3\x1b[3;1H\x1bD")
	wantLine(t, s, 0, "L0"+strings.Repeat(" ", 78))
	wantLine(t, s, 1, "L2"+strings.Repeat(" ", 78))
	wantLine(t, s, 2, strings.Repeat(" ", 80))
	wantLine(t, s, 3, "L3"+strings.Repeat(" ", 78))

	// RI at the top margin scrolls the region down.
	s = feedNew(t, "\x1b[2;3r\x1b[1;1HL0\x1b[2;1HL1\x1b[3;1HL2\x1b[4;1HL3\x1b[2;1H\x1bM")
	wantLine(t, s, 0, "L0"+strings.Repeat(" ", 78))
	wantLine(t, s, 1, strings.Repeat(" ", 80))
	wantLine(t, s, 2, "L1"+strings.Repeat(" ", 78))
	wantLine(t, s, 3, "L3"+strings.Repeat(" ", 78))

	// Off-margin IND/RI just move the cursor.
	s = feedNew(t, "\x1b[5;15r\x1b[8;1H\x1bD")
	wantCursor(t, s, 0, 8)
	NewStream(s).Feed("\x1bM\x1bM")
	wantCursor(t, s, 0, 6)
}

func TestMarginDegenerateAndReset(t *testing.T) {
	s := feedNew(t, "\x1b[7;7r") // Regions narrower than 2 are ignored.
	if _, ok := s.Margins(); ok {
		t.Error("degenerate margins should be ignored")
	}
	s = feedNew(t, "\x1b[5;15r\x1b[r")
	if _, ok := s.Margins(); ok {
		t.Error("CSI r should reset margins")
	}
	// An empty top parameter arrives as an explicit 0, which clamps to
	// the first line (the "keep the old edge" path is only reachable via
	// the direct API).
	s = feedNew(t, "\x1b[5;15r\x1b[;10r")
	if m, _ := s.Margins(); m != (Margins{0, 9}) {
		t.Errorf("margins = %+v, want {0 9}", m)
	}
	// Direct API with only a top argument keeps the current bottom.
	s = feedNew(t, "\x1b[5;15r")
	s.SetMargins(3)
	if m, _ := s.Margins(); m != (Margins{2, 14}) {
		t.Errorf("margins = %+v, want {2 14}", m)
	}
}

func TestInsertDeleteLines(t *testing.T) {
	// IL pushes lines down within the region and does a carriage return.
	s := feedNew(t, "A\r\nB\r\nC\x1b[2;5H\x1b[L")
	wantLine(t, s, 0, "A"+strings.Repeat(" ", 79))
	wantLine(t, s, 1, strings.Repeat(" ", 80))
	wantLine(t, s, 2, "B"+strings.Repeat(" ", 79))
	wantLine(t, s, 3, "C"+strings.Repeat(" ", 79))
	wantCursor(t, s, 0, 1)

	// DL pulls stored lines up.
	s = feedNew(t, "A\r\nB\r\nC\x1b[1;5H\x1b[M")
	wantLine(t, s, 0, "B"+strings.Repeat(" ", 79))
	wantLine(t, s, 1, "C"+strings.Repeat(" ", 79))
	wantCursor(t, s, 0, 0)

	// Outside the scrolling region both are noops.
	s = feedNew(t, "\x1b[5;10r\x1b[12;1HQ\r\x1b[L\x1b[M")
	wantLine(t, s, 11, "Q"+strings.Repeat(" ", 79))

	// pyte quirk: DL leaves the line as-is when the source line was
	// never stored.
	s = feedNew(t, "AAA\x1b[H\x1b[M")
	wantLine(t, s, 0, "AAA"+strings.Repeat(" ", 77))
}

func TestInsertDeleteCharacters(t *testing.T) {
	s := feedNew(t, "ABCDEF\r\x1b[3@")
	wantLine(t, s, 0, "   ABCDEF"+strings.Repeat(" ", 71))
	wantCursor(t, s, 0, 0)

	s = feedNew(t, "ABCDEF\r\x1b[2P")
	wantLine(t, s, 0, "CDEF"+strings.Repeat(" ", 76))

	// DCH beyond line length clears the rest.
	s = feedNew(t, "ABCDEF\r\x1b[999P")
	wantLine(t, s, 0, strings.Repeat(" ", 80))
}

func TestEraseCharacters(t *testing.T) {
	s := feedNew(t, "ABCDEF\r\x1b[44m\x1b[3X")
	wantLine(t, s, 0, "   DEF"+strings.Repeat(" ", 74))
	if got := s.At(0, 1).BG; got != "blue" {
		t.Errorf("erased cell BG = %q, want blue", got)
	}
	wantCursor(t, s, 0, 0)
}

func TestTabStops(t *testing.T) {
	s := feedNew(t, "\t")
	wantCursor(t, s, 8, 0)
	NewStream(s).Feed("\t\t")
	wantCursor(t, s, 24, 0)

	// HTS adds a stop; TBC 0 clears at the cursor; TBC 3 clears all.
	s = feedNew(t, "\x1b[5G\x1bH\r\t")
	wantCursor(t, s, 4, 0)
	s = feedNew(t, "\x1b[9G\x1b[g\r\t")
	wantCursor(t, s, 16, 0)
	s = feedNew(t, "\x1b[3g\r\t")
	wantCursor(t, s, 79, 0)

	got := feedNew(t, "").TabStops()
	if got[0] != 8 || got[len(got)-1] != 72 || len(got) != 9 {
		t.Errorf("default tabstops = %v", got)
	}
}

func TestDECOMAddressing(t *testing.T) {
	// With DECOM, CUP is relative to the top margin and clamped inside
	// the region.
	s := feedNew(t, "\x1b[5;15r\x1b[?6h\x1b[3;4H")
	wantCursor(t, s, 3, 6) // 4 (top) + 3 - 1.

	// Addressing outside the region is refused entirely.
	NewStream(s).Feed("\x1b[30;7H")
	wantCursor(t, s, 3, 6)

	// VPA under DECOM offsets by the top margin.
	NewStream(s).Feed("\x1b[2d")
	wantCursor(t, s, 3, 5)

	// VPA with a 0/default line selects line 1 (relative under DECOM).
	NewStream(s).Feed("\x1b[d")
	wantCursor(t, s, 3, 4)

	// Resetting DECOM homes the cursor and restores absolute addressing.
	NewStream(s).Feed("\x1b[?6l\x1b[2;2H")
	wantCursor(t, s, 1, 1)
}

func TestSaveRestoreCursor(t *testing.T) {
	s := feedNew(t, "\x1b[10;10H\x1b[1;31m\x1b7\x1b[H\x1b[0m\x1b8")
	wantCursor(t, s, 9, 9)
	c := s.Cursor()
	if !c.Attrs.Bold || c.Attrs.FG != "red" {
		t.Errorf("restored attrs = %+v, want bold red", c.Attrs)
	}

	// Empty stack: home + DECOM reset.
	s = feedNew(t, "\x1b[10;10H\x1b8")
	wantCursor(t, s, 0, 0)

	// Savepoints survive RIS (pyte quirk).
	s = feedNew(t, "\x1b[5;5H\x1b7\x1bc\x1b8")
	wantCursor(t, s, 4, 4)

	// A savepoint taken with DECOM set re-enables DECOM on restore (but,
	// like pyte, one taken with DECOM unset never disables it).
	s = feedNew(t, "\x1b[5;15r\x1b[?6h\x1b7\x1b[?6l\x1b8")
	if !containsInt(s.Modes(), DECOM) {
		t.Error("restore must re-enable saved DECOM")
	}
}

func TestDECCOLM(t *testing.T) {
	s := feedNew(t, "AB\x1b[?3h")
	if s.Columns() != 132 {
		t.Fatalf("columns = %d, want 132", s.Columns())
	}
	wantLine(t, s, 0, strings.Repeat(" ", 132)) // Screen erased.
	wantCursor(t, s, 0, 0)

	NewStream(s).Feed("\x1b[?3l")
	if s.Columns() != 80 {
		t.Fatalf("columns after reset = %d, want 80", s.Columns())
	}
}

func TestCharsetsDECSpecialGraphics(t *testing.T) {
	// With UseUTF8 (default), designation and shifts are ignored.
	s := feedNew(t, "\x1b(0lqk\x0eab\x0f")
	wantLine(t, s, 0, "lqkab"+strings.Repeat(" ", 75))

	// With UseUTF8 false, G0 DEC special graphics translate.
	s = NewScreen(80, 24)
	st := NewStream(s)
	st.UseUTF8 = false
	st.Feed("\x1b(0lqk\x1b(Bab")
	wantLine(t, s, 0, "┌─┐ab"+strings.Repeat(" ", 75))

	// G1 via SO/SI.
	s = NewScreen(80, 24)
	st = NewStream(s)
	st.UseUTF8 = false
	st.Feed("\x1b)0A\x0eq\x0fB")
	wantLine(t, s, 0, "A─B"+strings.Repeat(" ", 77))

	// Unknown designators are ignored.
	s = NewScreen(80, 24)
	st = NewStream(s)
	st.UseUTF8 = false
	st.Feed("\x1b(Zq")
	wantLine(t, s, 0, "q"+strings.Repeat(" ", 79))
}

func TestResizeClipping(t *testing.T) {
	// Shrinking lines drops from the top.
	s := NewScreen(80, 4)
	NewStream(s).Feed("L0\r\nL1\r\nL2\r\nL3")
	s.Resize(2, 0)
	if got := s.Display(); got[0] != "L2"+strings.Repeat(" ", 78) ||
		got[1] != "L3"+strings.Repeat(" ", 78) {
		t.Errorf("shrunk display = %q", got)
	}

	// Shrinking columns clips at the right; growing restores blanks.
	s = NewScreen(10, 2)
	NewStream(s).Feed("0123456789")
	s.Resize(0, 4)
	wantLine(t, s, 0, "0123")
	s.Resize(0, 8)
	wantLine(t, s, 0, "0123    ")

	// Identical size is a noop; margins reset otherwise.
	s = NewScreen(10, 5)
	NewStream(s).Feed("\x1b[2;4r")
	s.Resize(5, 10)
	if _, ok := s.Margins(); !ok {
		t.Error("noop resize must not touch margins")
	}
	s.Resize(4, 10)
	if _, ok := s.Margins(); ok {
		t.Error("real resize must reset margins")
	}
}

func TestAlignmentDisplay(t *testing.T) {
	s := feedSized(t, 4, 2, "\x1b[31mQ\x1b#8")
	wantLine(t, s, 0, "EEEE")
	wantLine(t, s, 1, "EEEE")
	if got := s.At(0, 0).FG; got != "red" {
		t.Errorf("DECALN must preserve attrs, FG = %q", got)
	}
}

func TestModeAccessorsAndDECSCNM(t *testing.T) {
	s := feedNew(t, "AB\x1b[?5h")
	if !s.DefaultChar().Reverse {
		t.Error("DECSCNM default char must be reverse")
	}
	if got := s.At(0, 0); !got.Reverse {
		t.Error("existing cells must be marked reverse")
	}
	if got := s.At(5, 5); !got.Reverse {
		t.Error("unset cells must render reverse")
	}
	if !containsInt(s.Modes(), DECSCNM) {
		t.Errorf("modes = %v, missing DECSCNM", s.Modes())
	}

	NewStream(s).Feed("\x1b[?5l")
	if s.At(0, 0).Reverse || s.DefaultChar().Reverse {
		t.Error("reset DECSCNM must clear reverse")
	}
}

func TestCursorVisibility(t *testing.T) {
	s := feedNew(t, "\x1b[?25l")
	if !s.Cursor().Hidden {
		t.Error("DECTCEM reset must hide the cursor")
	}
	NewStream(s).Feed("\x1b[?25h")
	if s.Cursor().Hidden {
		t.Error("DECTCEM set must show the cursor")
	}
}

func TestReportDeviceAttributesAndStatus(t *testing.T) {
	var got []string
	s := NewScreen(80, 24)
	s.WriteProcessInput = func(data string) { got = append(got, data) }
	st := NewStream(s)

	st.Feed("\x1b[c\x1b[?1c\x1b[5n\x1b[10;20H\x1b[6n\x1b[7n")
	want := []string{"\x1b[?6c", "\x1b[0n", "\x1b[10;20R"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("process input = %q, want %q", got, want)
	}

	// DSR 6 under DECOM reports margin-relative coordinates.
	got = nil
	st.Feed("\x1b[5;15r\x1b[?6h\x1b[3;4H\x1b[6n")
	if !reflect.DeepEqual(got, []string{"\x1b[3;4R"}) {
		t.Errorf("DECOM DSR = %q", got)
	}

	// Without a sink, reports are silently dropped.
	s2 := feedNew(t, "\x1b[c\x1b[6n")
	wantCursor(t, s2, 0, 0)
}

func TestBufferViewAccessors(t *testing.T) {
	s := feedNew(t, "AB\x1b[3;1HZ")
	v := s.Buffer()
	if got := v.At(0, 1).Data; got != "B" {
		t.Errorf("At(0,1) = %q", got)
	}
	if got := v.Rows(); !reflect.DeepEqual(got, []int{0, 2}) {
		t.Errorf("Rows() = %v, want [0 2]", got)
	}
	if got := v.StoredColumns(0); !reflect.DeepEqual(got, []int{0, 1}) {
		t.Errorf("StoredColumns(0) = %v, want [0 1]", got)
	}
	if got := v.StoredColumns(7); got != nil {
		t.Errorf("StoredColumns(7) = %v, want nil", got)
	}
}

func TestResetAndTitle(t *testing.T) {
	s := feedNew(t, "\x1b]0;my title\x07junk\x1b[5;15r\x1b[?5h")
	if s.Title() != "my title" || s.IconName() != "my title" {
		t.Errorf("title/icon = %q/%q", s.Title(), s.IconName())
	}
	s.Reset()
	if s.Title() != "" || s.IconName() != "" {
		t.Error("reset must clear title and icon name")
	}
	if _, ok := s.Margins(); ok {
		t.Error("reset must clear margins")
	}
	if got := s.Modes(); !reflect.DeepEqual(got, []int{DECAWM, DECTCEM}) {
		t.Errorf("modes after reset = %v", got)
	}
	wantLine(t, s, 0, strings.Repeat(" ", 80))
}

func TestWideCharacters(t *testing.T) {
	s := feedNew(t, "你a")
	wantLine(t, s, 0, "你a"+strings.Repeat(" ", 77))
	wantCursor(t, s, 3, 0)
	if got := s.At(0, 1).Data; got != "" {
		t.Errorf("stub cell data = %q, want empty", got)
	}

	// Wide char at the last column gets no stub and lands the cursor in
	// the phantom column.
	s = feedNew(t, strings.Repeat(" ", 79)+"你")
	wantCursor(t, s, 80, 0)
	if got := s.At(0, 79).Data; got != "你" {
		t.Errorf("cell(0,79) = %q", got)
	}

	// Orphaned stubs render as nothing (pyte would crash here).
	s = feedNew(t, "你\rA")
	wantLine(t, s, 0, "A"+strings.Repeat(" ", 78))
}

func TestCombiningCharacters(t *testing.T) {
	s := feedNew(t, "éx")
	wantLine(t, s, 0, "éx"+strings.Repeat(" ", 78))

	// At column 0, the mark merges into the last cell of the previous line.
	s = feedNew(t, "ab\r\ń")
	if got := s.At(0, 79).Data; got != " ́" {
		t.Errorf("previous-line merge = %q", got)
	}

	// At the origin the mark is dropped.
	s = feedNew(t, "́Z")
	wantLine(t, s, 0, "Z"+strings.Repeat(" ", 79))
}

func TestInsertModeIRM(t *testing.T) {
	s := feedNew(t, "ABCDEF\r\x1b[4hXY")
	wantLine(t, s, 0, "XYABCDEF"+strings.Repeat(" ", 72))
	NewStream(s).Feed("\x1b[4lZ")
	wantLine(t, s, 0, "XYZBCDEF"+strings.Repeat(" ", 72))
}

func TestLNM(t *testing.T) {
	s := feedNew(t, "AB\x1b[20hCD\nEF")
	wantLine(t, s, 1, "EF"+strings.Repeat(" ", 78))
	wantCursor(t, s, 2, 1)

	s = feedNew(t, "AB\nCD")
	wantLine(t, s, 1, "  CD"+strings.Repeat(" ", 76))
}

func TestBellIsANoop(t *testing.T) {
	s := feedNew(t, "a\x07b")
	wantLine(t, s, 0, "ab"+strings.Repeat(" ", 78))
	s.Bell() // Direct call is a noop too.
	wantCursor(t, s, 2, 0)
}
