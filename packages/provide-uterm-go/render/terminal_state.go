//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package render

import (
	"fmt"
	"strings"
	"unicode/utf8"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/vt"
)

// The terminal state a screen snapshot has to carry besides its cells. Port of
// provide.uterm.render.terminal_state, emitting the same bytes.
//
// A viewer that joins a live session is painted from a snapshot and then fed
// the rest of the stream, and that stream does not start over for it: each
// byte assumes the cursor, pen and modes the bytes before it left. A program
// that paints with relative movement (vitetris never sends CUP) drew its whole
// board in the wrong place on a viewer whose snapshot left the cursor at the
// bottom-right of the painted rows, and in the default background on one that
// never saw its last SGR.

// decckm is application cursor keys, shifted as vt stores private modes.
const decckm = 1 << 5

// altScreenModes are the alternate-screen modes, in the order re-entered.
var altScreenModes = []int{47, 1047, 1049}

// inputModes change what a controlling viewer's keyboard and mouse send.
// They are restored only when set.
var inputModes = []int{1000, 1002, 1003, 1005, 1006, 1015, 2004}

// PenSGR returns the SGR sequence that sets a fresh terminal's pen to attrs:
// from a reset, every flag, then both colours. Reverse stays reverse.
func PenSGR(attrs vt.Char) string {
	codes := []int{0}
	for _, flag := range []struct {
		on   bool
		code int
	}{
		{attrs.Bold, 1}, {attrs.Italics, 3}, {attrs.Underscore, 4},
		{attrs.Blink, 5}, {attrs.Reverse, 7}, {attrs.Strikethrough, 9},
	} {
		if flag.on {
			codes = append(codes, flag.code)
		}
	}
	style := CellStyle(attrs)
	codes = append(codes, colorSGR(style.FG, true)...)
	codes = append(codes, colorSGR(style.BG, false)...)
	return sgrSequence(codes)
}

// ScreenStatePrefix is what must precede the painted rows: the alternate
// screen, if active. It cannot follow them, because entering it clears.
func ScreenStatePrefix(s *vt.Screen) string {
	var entered strings.Builder
	for _, mode := range altScreenModes {
		if s.HasMode(mode << 5) {
			fmt.Fprintf(&entered, "\x1b[?%dh", mode)
		}
	}
	if entered.Len() == 0 {
		return ""
	}
	return entered.String() + "\x1b[H"
}

// lastCellRedraw is the column and bytes that redraw row y's last cell,
// leaving the cursor deferred: "<column>H<pen><text>", for a caller to prefix
// with ESC [ <row> ;.
//
// The cell is drawn again, in its own style, from the column before; a wide
// character there occupies the last two cells and is redrawn whole. Marks in
// the cell, or in a wide character's second cell, are written after it with
// autowrap off: they join the cell before the cursor, which rests past the
// last column, and the suffix has already turned autowrap on, so they would
// otherwise wrap to the next row first.
//
// A last cell holding a second half whose first half is gone, or the second
// half of a wide character with marks of its own, cannot be drawn that way:
// with exact this reports false for it, and otherwise the nearest thing. Port
// of _last_cell_redraw.
func lastCellRedraw(s *vt.Screen, y int, exact bool) (string, bool) {
	x := s.Columns() - 1
	marks := ""
	if last := s.At(y, x).Data; x > 0 && isSecondHalf(last) {
		before := s.At(y, x-1).Data
		if exact && (utf8.RuneCountInString(before) != 1 || vt.CellWidth(before) != 2) {
			return "", false
		}
		marks = last
		x--
	} else if exact && isSecondHalf(last) {
		return "", false
	}
	cell := s.At(y, x)
	text := cell.Data
	if text == "" {
		text = " "
	}
	_, size := utf8.DecodeRuneInString(text)
	redraw := fmt.Sprintf("%dH%s%s", x+1, StyleToSGR(CellStyle(cell)), text[:size])
	if held := text[size:] + marks; held != "" {
		return redraw + "\x1b[?7l" + held + "\x1b[?7h", true
	}
	return redraw, true
}

// cursorPlacement moves to the cursor, reproducing the deferred wrap at the
// last column: a cursor that has just drawn there rests at X == columns,
// which no CUP can address, so the row's last cell is drawn again (see
// lastCellRedraw), which leaves any terminal in the same state.
//
// Some last cells cannot be drawn so: a second half whose first half is gone,
// or one of a wide character with marks of its own. A cursor gets there
// deferred when CUU, CUD, IND or RI move it from another row, all of which
// keep the column. So that is done here too: the cursor is left deferred past
// a neighbouring row's last cell, redrawn as it is, and moved onto this row
// with CUD from the row above or CUU from the row below. Either must land on
// this row despite the scrolling region, and in origin mode the neighbour
// must be inside it. Only with no such neighbour (a one-row screen, or
// neighbours that cannot be drawn either) is the last cell drawn as nearly as
// it can be. Port of _cursor_placement.
func cursorPlacement(s *vt.Screen, row int) string {
	cursor := s.Cursor()
	if cursor.X < s.Columns() {
		return fmt.Sprintf("\x1b[%d;%dH", row, cursor.X+1)
	}
	y := cursor.Y
	if redraw, ok := lastCellRedraw(s, y, true); ok {
		return fmt.Sprintf("\x1b[%d;%s", row, redraw)
	}
	top, bottom := 0, s.Lines()-1
	if m, ok := s.Margins(); ok {
		top, bottom = m.Top, m.Bottom
	}
	origin := s.HasMode(vt.DECOM)
	neighbours := []struct {
		row   int
		move  string
		lands bool
	}{{y - 1, "\x1b[B", y <= bottom}, {y + 1, "\x1b[A", y >= top}}
	for _, n := range neighbours {
		if !n.lands || n.row < 0 || n.row >= s.Lines() || (origin && (n.row < top || n.row > bottom)) {
			continue
		}
		if redraw, ok := lastCellRedraw(s, n.row, true); ok {
			return fmt.Sprintf("\x1b[%d;%s%s", row+n.row-y, redraw, n.move)
		}
	}
	redraw, _ := lastCellRedraw(s, y, false)
	return fmt.Sprintf("\x1b[%d;%s", row, redraw)
}

// charsetState is the G0/G1 designations and the shift, unless nothing has
// touched them. The power-on sets (G0 "B", G1 "0", shifted in) are sent once
// the stream has designated any: a viewer's G1 powers on as ASCII, so a
// program that designated G1 as graphics before it joined would otherwise draw
// letters at its next SO. Untouched, nothing is sent, because a snapshot is
// also read as text and strip_ansi leaves designations in.
func charsetState(s *vt.Screen) string {
	g0, g1, shifted := s.Charsets()
	if g0 == "B" && g1 == "0" && !shifted && !s.CharsetsDesignated() {
		return ""
	}
	shift := ""
	if shifted {
		shift = "\x0e"
	}
	return "\x1b(" + g0 + "\x1b)" + g1 + shift
}

// ScreenStateSuffix is what must follow the painted rows to leave a terminal
// in s's state. charsets says whether the stream honours designations and
// SO/SI at all (vt.Stream.UseUTF8 off); a stream that ignores them has no
// such state to restore.
//
// The order is load-bearing: DECSTBM and DECOM both home the cursor, so they
// come before it is placed; the deferred-wrap redraw needs autowrap on and
// insert mode off, so their real values come after it.
func ScreenStateSuffix(s *vt.Screen, charsets bool) string {
	var out strings.Builder
	margins, ok := s.Margins()
	if ok && margins == (vt.Margins{Top: 0, Bottom: s.Lines() - 1}) {
		ok = false
	}
	if ok {
		fmt.Fprintf(&out, "\x1b[%d;%dr", margins.Top+1, margins.Bottom+1)
	} else {
		out.WriteString("\x1b[r")
	}
	origin := s.HasMode(vt.DECOM)
	if origin {
		out.WriteString("\x1b[?6h")
	} else {
		out.WriteString("\x1b[?6l")
	}
	out.WriteString("\x1b[?7h\x1b[4l")
	row := s.Cursor().Y + 1
	if origin && ok {
		row -= margins.Top
	}
	out.WriteString(cursorPlacement(s, row))
	if !s.HasMode(vt.DECAWM) {
		out.WriteString("\x1b[?7l")
	}
	if s.HasMode(vt.IRM) {
		out.WriteString("\x1b[4h")
	}
	if s.HasMode(vt.LNM) {
		out.WriteString("\x1b[20h")
	} else {
		out.WriteString("\x1b[20l")
	}
	if s.HasMode(decckm) {
		out.WriteString("\x1b[?1h")
	} else {
		out.WriteString("\x1b[?1l")
	}
	for _, mode := range inputModes {
		if s.HasMode(mode << 5) {
			fmt.Fprintf(&out, "\x1b[?%dh", mode)
		}
	}
	if charsets {
		out.WriteString(charsetState(s))
	}
	out.WriteString(PenSGR(s.Cursor().Attrs))
	if s.Cursor().Hidden {
		out.WriteString(ANSIHideCursor)
	} else {
		out.WriteString(ANSIShowCursor)
	}
	return out.String()
}
