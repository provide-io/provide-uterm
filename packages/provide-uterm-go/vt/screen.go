//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

import (
	"fmt"
	"sort"
	"strings"
	"unicode/utf8"
)

// Screen is an in-memory matrix of characters representing a terminal
// display, mirroring pyte.screens.Screen. The buffer is sparse: unset
// cells render as spaces with default attributes (reverse video when
// DECSCNM is active).
type Screen struct {
	// WriteProcessInput, when non-nil, receives responses the terminal
	// would write back to the attached process (DA / DSR reports).
	WriteProcessInput func(data string)

	columns, lines int
	buffer         map[int]map[int]Char
	mode           map[int]bool
	margins        *Margins
	title          string
	iconName       string
	charset        int
	g0Charset      *charsetMap
	g1Charset      *charsetMap
	tabstops       map[int]bool
	cursor         Cursor
	savepoints     []savepoint
	savedColumns   int // -1 when unset (pyte's saved_columns = None)
}

// NewScreen creates a screen with the given dimensions and resets it to
// the initial state.
func NewScreen(columns, lines int) *Screen {
	s := &Screen{columns: columns, lines: lines}
	s.Reset()
	return s
}

// Columns returns the current number of columns.
func (s *Screen) Columns() int { return s.columns }

// Lines returns the current number of lines.
func (s *Screen) Lines() int { return s.lines }

// Cursor returns a copy of the current cursor state.
func (s *Screen) Cursor() Cursor { return s.cursor }

// Title returns the terminal title set via OSC.
func (s *Screen) Title() string { return s.title }

// IconName returns the icon name set via OSC.
func (s *Screen) IconName() string { return s.iconName }

// Margins returns the DECSTBM scrolling region and whether one is set.
func (s *Screen) Margins() (Margins, bool) {
	if s.margins == nil {
		return Margins{}, false
	}
	return *s.margins, true
}

// Modes returns the currently set terminal modes, sorted ascending.
// Private (DEC) modes appear shifted left by 5, as in pyte.
func (s *Screen) Modes() []int {
	out := make([]int, 0, len(s.mode))
	for m := range s.mode {
		out = append(out, m)
	}
	sort.Ints(out)
	return out
}

// TabStops returns the current tab stop columns, sorted ascending.
func (s *Screen) TabStops() []int {
	out := make([]int, 0, len(s.tabstops))
	for t := range s.tabstops {
		out = append(out, t)
	}
	sort.Ints(out)
	return out
}

// DefaultChar returns the character unset cells render as: a space with
// default colors, reverse when DECSCNM screen-wide reverse video is on.
func (s *Screen) DefaultChar() Char { return s.defaultChar() }

func (s *Screen) defaultChar() Char {
	c := defaultCharPlain
	c.Reverse = s.mode[DECSCNM]
	return c
}

// line returns the stored line map for y, creating it when absent —
// mirroring pyte's defaultdict buffer access.
func (s *Screen) line(y int) map[int]Char {
	if l, ok := s.buffer[y]; ok {
		return l
	}
	l := make(map[int]Char)
	s.buffer[y] = l
	return l
}

// At returns the rendered cell at 0-based (y, x): the stored cell, or the
// default character when unset. It never mutates the buffer.
func (s *Screen) At(y, x int) Char {
	if l, ok := s.buffer[y]; ok {
		if c, ok := l[x]; ok {
			return c
		}
	}
	return s.defaultChar()
}

// BufferView provides read access to the sparse cell matrix.
type BufferView struct {
	s *Screen
}

// Buffer returns a read-only view of the sparse cell buffer.
func (s *Screen) Buffer() BufferView { return BufferView{s: s} }

// At returns the rendered cell at 0-based (y, x).
func (v BufferView) At(y, x int) Char { return v.s.At(y, x) }

// Rows returns the row indices that have explicitly stored cells or were
// materialized by screen operations, sorted ascending.
func (v BufferView) Rows() []int {
	out := make([]int, 0, len(v.s.buffer))
	for y := range v.s.buffer {
		out = append(out, y)
	}
	sort.Ints(out)
	return out
}

// StoredColumns returns the column indices explicitly stored in row y,
// sorted ascending. Cells not listed render as the default character.
func (v BufferView) StoredColumns(y int) []int {
	l, ok := v.s.buffer[y]
	if !ok {
		return nil
	}
	out := make([]int, 0, len(l))
	for x := range l {
		out = append(out, x)
	}
	sort.Ints(out)
	return out
}

// Display returns the screen lines as strings. Wide characters occupy two
// columns, so lines containing them have fewer code points than columns.
// Like pyte's display property, this materializes every screen row.
func (s *Screen) Display() []string {
	out := make([]string, s.lines)
	for y := 0; y < s.lines; y++ {
		line := s.line(y)
		def := s.defaultChar()
		var b strings.Builder
		isWide := false
		for x := 0; x < s.columns; x++ {
			if isWide { // Skip the stub cell after a wide character.
				isWide = false
				continue
			}
			data := cellAt(line, x, def).Data
			if data != "" {
				r, _ := utf8.DecodeRuneInString(data)
				isWide = runeWidth(r) == 2
			}
			b.WriteString(data)
		}
		out[y] = b.String()
	}
	return out
}

// Reset restores the initial state: buffer cleared, margins and modes
// reset, tab stops every 8 columns, cursor homed with default attributes.
// Savepoints survive a reset, exactly as in pyte.
func (s *Screen) Reset() {
	s.buffer = make(map[int]map[int]Char)
	s.margins = nil
	s.mode = map[int]bool{DECAWM: true, DECTCEM: true}
	s.title = ""
	s.iconName = ""
	s.charset = 0
	s.g0Charset = &lat1Map
	s.g1Charset = &vt100Map
	s.tabstops = make(map[int]bool)
	for x := 8; x < s.columns; x += 8 {
		s.tabstops[x] = true
	}
	s.cursor = Cursor{Attrs: defaultCharPlain}
	s.CursorPosition(0, 0)
	s.savedColumns = -1
}

// Resize changes the screen size. Extra lines are clipped from the top,
// extra columns from the right; margins are reset to the full screen.
// Passing 0 keeps the corresponding current dimension.
func (s *Screen) Resize(lines, columns int) {
	if lines == 0 {
		lines = s.lines
	}
	if columns == 0 {
		columns = s.columns
	}
	if lines == s.lines && columns == s.columns {
		return
	}

	if lines < s.lines {
		s.SaveCursor()
		s.CursorPosition(0, 0)
		s.DeleteLines(s.lines - lines) // Drop lines from the top.
		s.RestoreCursor()
	}

	if columns < s.columns {
		for _, l := range s.buffer {
			for x := columns; x < s.columns; x++ {
				delete(l, x)
			}
		}
	}

	s.lines, s.columns = lines, columns
	s.SetMargins()
}

// SetMargins selects the scrolling region (DECSTBM). Arguments are
// optional 1-based top and bottom lines; with no arguments (or top 0 and
// no bottom) the margins reset to the whole screen.
func (s *Screen) SetMargins(params ...int) {
	haveTop := len(params) >= 1
	haveBottom := len(params) >= 2

	// 0 corresponds to CSI r with no parameters.
	if (!haveTop || params[0] == 0) && !haveBottom {
		s.margins = nil
		return
	}

	cur := s.margins
	if cur == nil {
		cur = &Margins{Top: 0, Bottom: s.lines - 1}
	}

	top := cur.Top
	if haveTop {
		top = max(0, min(params[0]-1, s.lines-1))
	}
	bottom := cur.Bottom
	if haveBottom {
		bottom = max(0, min(params[1]-1, s.lines-1))
	}

	// Regions of width less than 2 are ignored (like pyte, which keeps
	// this VT102-mandated check despite programs that rely on 1-line
	// regions).
	if bottom-top >= 1 {
		s.margins = &Margins{Top: top, Bottom: bottom}
		// The cursor moves home when the scrolling region changes.
		s.CursorPosition(0, 0)
	}
}

// marginsOrScreen returns the scrolling region, defaulting to the whole
// screen when no margins are set.
func (s *Screen) marginsOrScreen() (top, bottom int) {
	if s.margins != nil {
		return s.margins.Top, s.margins.Bottom
	}
	return 0, s.lines - 1
}

// SetMode sets (enables) the given modes; private selects the DEC private
// mode space (CSI ? ... h), shifting each mode code left by 5.
func (s *Screen) SetMode(private bool, modes ...int) {
	ml := make([]int, len(modes))
	copy(ml, modes)
	if private {
		for i := range ml {
			ml[i] <<= 5
		}
	}

	for _, m := range ml {
		s.mode[m] = true
	}

	// When DECCOLM is set, the screen is erased and the cursor homed.
	if containsInt(ml, DECCOLM) {
		s.savedColumns = s.columns
		s.Resize(0, 132)
		s.EraseInDisplay(2)
		s.CursorPosition(0, 0)
	}

	// DECOM homes the cursor (VT520 manual).
	if containsInt(ml, DECOM) {
		s.CursorPosition(0, 0)
	}

	// Mark all displayed characters as reverse.
	if containsInt(ml, DECSCNM) {
		for _, l := range s.buffer {
			for x, c := range l {
				c.Reverse = true
				l[x] = c
			}
		}
		s.SelectGraphicRendition(7) // +reverse
	}

	// Make the cursor visible.
	if containsInt(ml, DECTCEM) {
		s.cursor.Hidden = false
	}
}

// ResetMode resets (disables) the given modes; see SetMode.
func (s *Screen) ResetMode(private bool, modes ...int) {
	ml := make([]int, len(modes))
	copy(ml, modes)
	if private {
		for i := range ml {
			ml[i] <<= 5
		}
	}

	for _, m := range ml {
		delete(s.mode, m)
	}

	if containsInt(ml, DECCOLM) {
		if s.columns == 132 && s.savedColumns != -1 {
			s.Resize(0, s.savedColumns)
			s.savedColumns = -1
		}
		s.EraseInDisplay(2)
		s.CursorPosition(0, 0)
	}

	if containsInt(ml, DECOM) {
		s.CursorPosition(0, 0)
	}

	if containsInt(ml, DECSCNM) {
		for _, l := range s.buffer {
			for x, c := range l {
				c.Reverse = false
				l[x] = c
			}
		}
		s.SelectGraphicRendition(27) // -reverse
	}

	// Hide the cursor.
	if containsInt(ml, DECTCEM) {
		s.cursor.Hidden = true
	}
}

// DefineCharset defines the G0 ("(") or G1 (")") charset. Unknown charset
// codes are ignored.
func (s *Screen) DefineCharset(code, mode string) {
	m, ok := charsetMaps[code]
	if !ok {
		return
	}
	switch mode {
	case "(":
		s.g0Charset = m
	case ")":
		s.g1Charset = m
	}
}

// ShiftIn selects the G0 character set.
func (s *Screen) ShiftIn() { s.charset = 0 }

// ShiftOut selects the G1 character set.
func (s *Screen) ShiftOut() { s.charset = 1 }

// SetTitle sets the terminal title (OSC 2 / OSC 0).
func (s *Screen) SetTitle(param string) { s.title = param }

// SetIconName sets the icon name (OSC 1 / OSC 0).
func (s *Screen) SetIconName(param string) { s.iconName = param }

// Bell is a stub; hook behavior up externally if needed.
func (s *Screen) Bell() {}

// AlignmentDisplay fills the screen with uppercase E's (DECALN),
// preserving existing cell attributes.
func (s *Screen) AlignmentDisplay() {
	for y := 0; y < s.lines; y++ {
		line := s.line(y)
		def := s.defaultChar()
		for x := 0; x < s.columns; x++ {
			line[x] = withData(cellAt(line, x, def), "E")
		}
	}
}

// ReportDeviceAttributes reports the terminal identity (primary DA only,
// as VT102). Private requests are ignored.
func (s *Screen) ReportDeviceAttributes(mode int, private bool) {
	if mode == 0 && !private {
		s.writeProcessInput("\x1b[?6c")
	}
}

// ReportDeviceStatus reports terminal status (5) or cursor position (6);
// other modes are a noop.
func (s *Screen) ReportDeviceStatus(mode int) {
	switch mode {
	case 5:
		s.writeProcessInput("\x1b[0n")
	case 6:
		x := s.cursor.X + 1
		y := s.cursor.Y + 1
		// Origin mode (DECOM) selects line numbering.
		if s.mode[DECOM] && s.margins != nil {
			y -= s.margins.Top
		}
		s.writeProcessInput(fmt.Sprintf("\x1b[%d;%dR", y, x))
	}
}

func (s *Screen) writeProcessInput(data string) {
	if s.WriteProcessInput != nil {
		s.WriteProcessInput(data)
	}
}

// containsInt reports whether v is present in xs.
func containsInt(xs []int, v int) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}
