//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

// Cursor movement, addressing, tab stops and save/restore.
//
// For every method taking a count or coordinate, 0 selects the default
// (mirroring pyte, where a 0 CSI parameter behaves like an omitted one).

// ensureHBounds clamps the cursor within horizontal screen bounds.
func (s *Screen) ensureHBounds() {
	s.cursor.X = min(max(0, s.cursor.X), s.columns-1)
}

// ensureVBounds clamps the cursor within vertical screen bounds; when
// useMargins is true or DECOM is set, the margins bound the cursor instead.
func (s *Screen) ensureVBounds(useMargins bool) {
	top, bottom := 0, s.lines-1
	if (useMargins || s.mode[DECOM]) && s.margins != nil {
		top, bottom = s.margins.Top, s.margins.Bottom
	}
	s.cursor.Y = min(max(top, s.cursor.Y), bottom)
}

// CursorUp moves the cursor up count lines in the same column, stopping
// at the top margin.
func (s *Screen) CursorUp(count int) {
	if count == 0 {
		count = 1
	}
	top, _ := s.marginsOrScreen()
	s.cursor.Y = max(s.cursor.Y-count, top)
}

// CursorUp1 moves the cursor up count lines to column 1.
func (s *Screen) CursorUp1(count int) {
	s.CursorUp(count)
	s.CarriageReturn()
}

// CursorDown moves the cursor down count lines in the same column,
// stopping at the bottom margin.
func (s *Screen) CursorDown(count int) {
	if count == 0 {
		count = 1
	}
	_, bottom := s.marginsOrScreen()
	s.cursor.Y = min(s.cursor.Y+count, bottom)
}

// CursorDown1 moves the cursor down count lines to column 1.
func (s *Screen) CursorDown1(count int) {
	s.CursorDown(count)
	s.CarriageReturn()
}

// CursorBack moves the cursor left count columns, stopping at the left
// margin.
func (s *Screen) CursorBack(count int) {
	// Handle the case when the previous draw ended in the last column
	// and the cursor sits in the phantom position past it.
	if s.cursor.X == s.columns {
		s.cursor.X--
	}

	if count == 0 {
		count = 1
	}
	s.cursor.X -= count
	s.ensureHBounds()
}

// CursorForward moves the cursor right count columns, stopping at the
// right margin.
func (s *Screen) CursorForward(count int) {
	if count == 0 {
		count = 1
	}
	s.cursor.X += count
	s.ensureHBounds()
}

// CursorPosition moves the cursor to a 1-based (line, column); 0 selects
// 1. With DECOM set, line numbers are relative to the top margin and the
// cursor cannot leave the scrolling region.
func (s *Screen) CursorPosition(line, column int) {
	if column == 0 {
		column = 1
	}
	column--
	if line == 0 {
		line = 1
	}
	line--

	if s.margins != nil && s.mode[DECOM] {
		line += s.margins.Top
		// The cursor is not allowed to move out of the scrolling region.
		if line < s.margins.Top || line > s.margins.Bottom {
			return
		}
	}

	s.cursor.X = column
	s.cursor.Y = line
	s.ensureHBounds()
	s.ensureVBounds(false)
}

// CursorToColumn moves the cursor to a 1-based column in the current line.
func (s *Screen) CursorToColumn(column int) {
	if column == 0 {
		column = 1
	}
	s.cursor.X = column - 1
	s.ensureHBounds()
}

// CursorToLine moves the cursor to a 1-based line in the current column.
// With DECOM set, the line is relative to the top margin.
func (s *Screen) CursorToLine(line int) {
	if line == 0 {
		line = 1
	}
	s.cursor.Y = line - 1

	if s.mode[DECOM] && s.margins != nil {
		s.cursor.Y += s.margins.Top
	}

	s.ensureVBounds(false)
}

// CarriageReturn moves the cursor to the beginning of the current line.
func (s *Screen) CarriageReturn() { s.cursor.X = 0 }

// Backspace moves the cursor one column left, stopping at the beginning
// of the line.
func (s *Screen) Backspace() { s.CursorBack(0) }

// Tab moves the cursor to the next tab stop, or the last column when
// there are no more stops.
func (s *Screen) Tab() {
	column := s.columns - 1
	for _, stop := range s.TabStops() {
		if s.cursor.X < stop {
			column = stop
			break
		}
	}
	s.cursor.X = column
}

// SetTabStop sets a horizontal tab stop at the cursor position.
func (s *Screen) SetTabStop() { s.tabstops[s.cursor.X] = true }

// ClearTabStop clears the tab stop at the cursor position (how 0) or all
// tab stops (how 3).
func (s *Screen) ClearTabStop(how int) {
	switch how {
	case 0:
		delete(s.tabstops, s.cursor.X)
	case 3:
		s.tabstops = make(map[int]bool)
	}
}

// SaveCursor pushes the cursor position, attributes, charset state and
// origin/wrap modes onto the savepoint stack (DECSC).
func (s *Screen) SaveCursor() {
	s.savepoints = append(s.savepoints, savepoint{
		cursor:  s.cursor,
		g0:      s.g0Charset,
		g1:      s.g1Charset,
		charset: s.charset,
		origin:  s.mode[DECOM],
		wrap:    s.mode[DECAWM],
	})
}

// RestoreCursor pops the last savepoint (DECRC). With an empty stack the
// cursor moves home and origin mode resets. Note that, like pyte, modes
// saved as unset are not reset on restore — they are only re-enabled when
// the savepoint recorded them as set.
func (s *Screen) RestoreCursor() {
	if n := len(s.savepoints); n > 0 {
		sp := s.savepoints[n-1]
		s.savepoints = s.savepoints[:n-1]

		s.g0Charset = sp.g0
		s.g1Charset = sp.g1
		s.charset = sp.charset

		if sp.origin {
			s.SetMode(false, DECOM)
		}
		if sp.wrap {
			s.SetMode(false, DECAWM)
		}

		s.cursor = sp.cursor
		s.ensureHBounds()
		s.ensureVBounds(true)
		return
	}

	// If nothing was saved, the cursor moves home and origin mode resets.
	s.ResetMode(false, DECOM)
	s.CursorPosition(0, 0)
}
