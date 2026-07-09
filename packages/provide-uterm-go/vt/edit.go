//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

// Drawing, scrolling, and line/character insertion, deletion and erasure.
// Each method mirrors the corresponding pyte.screens.Screen handler,
// including its sparse-buffer quirks: erase operations rewrite only cells
// that are explicitly stored, and scroll/shift operations move whole line
// maps around.

// Draw displays characters at the cursor position, advancing the cursor
// and wrapping at the right margin when DECAWM is set. Zero-width
// combining characters merge into the preceding cell; other unprintable
// characters abort the rest of the batch, exactly like pyte.
func (s *Screen) Draw(data string) {
	cs := s.g0Charset
	if s.charset == 1 {
		cs = s.g1Charset
	}

	for _, char := range data {
		if char >= 0 && char < 256 {
			char = cs[char]
		}
		charWidth := runeWidth(char)

		// If this was the last column in a line and auto wrap mode is
		// enabled, move the cursor to the beginning of the next line,
		// otherwise replace characters already displayed.
		if s.cursor.X == s.columns {
			if s.mode[DECAWM] {
				s.CarriageReturn()
				s.LineFeed()
			} else if charWidth > 0 {
				s.cursor.X -= charWidth
			}
		}

		// In insert mode new characters shift old ones to the right.
		if s.mode[IRM] && charWidth > 0 {
			s.InsertCharacters(charWidth)
		}

		line := s.line(s.cursor.Y)
		switch {
		case charWidth == 1:
			line[s.cursor.X] = withData(s.cursor.Attrs, string(char))
		case charWidth == 2:
			// A two-cell character has a stub slot after it.
			line[s.cursor.X] = withData(s.cursor.Attrs, string(char))
			if s.cursor.X+1 < s.columns {
				line[s.cursor.X+1] = withData(s.cursor.Attrs, "")
			}
		case charWidth == 0 && combiningClass(char) > 0:
			// A zero-cell character combines with the previous character
			// either on this or the preceding line.
			if s.cursor.X > 0 {
				last := cellAt(line, s.cursor.X-1, s.defaultChar())
				normalized := nfcNormalize(last.Data + string(char))
				line[s.cursor.X-1] = withData(last, normalized)
			} else if s.cursor.Y > 0 {
				prev := s.line(s.cursor.Y - 1)
				last := cellAt(prev, s.columns-1, s.defaultChar())
				normalized := nfcNormalize(last.Data + string(char))
				prev[s.columns-1] = withData(last, normalized)
			}
		default:
			// Unprintable character, or one that does not advance the
			// cursor: stop processing the rest of the batch.
			return
		}

		if charWidth > 0 {
			s.cursor.X = min(s.cursor.X+charWidth, s.columns)
		}
	}
}

// Index moves the cursor down one line in the same column; at the bottom
// margin the scrolling region scrolls up.
func (s *Screen) Index() {
	top, bottom := s.marginsOrScreen()
	if s.cursor.Y == bottom {
		for y := top; y < bottom; y++ {
			if src, ok := s.buffer[y+1]; ok {
				s.buffer[y] = src
			} else {
				s.buffer[y] = make(map[int]Char)
			}
		}
		delete(s.buffer, bottom)
	} else {
		s.CursorDown(0)
	}
}

// ReverseIndex moves the cursor up one line in the same column; at the
// top margin the scrolling region scrolls down.
func (s *Screen) ReverseIndex() {
	top, bottom := s.marginsOrScreen()
	if s.cursor.Y == top {
		for y := bottom; y > top; y-- {
			if src, ok := s.buffer[y-1]; ok {
				s.buffer[y] = src
			} else {
				s.buffer[y] = make(map[int]Char)
			}
		}
		delete(s.buffer, top)
	} else {
		s.CursorUp(0)
	}
}

// LineFeed performs an Index and, when LNM is set, a carriage return.
func (s *Screen) LineFeed() {
	s.Index()
	if s.mode[LNM] {
		s.CarriageReturn()
	}
}

// InsertLines inserts count blank lines at the cursor line; lines at and
// below the cursor move down, and lines pushed past the bottom margin are
// lost. A noop when the cursor is outside the scrolling region.
func (s *Screen) InsertLines(count int) {
	if count == 0 {
		count = 1
	}
	top, bottom := s.marginsOrScreen()

	if top <= s.cursor.Y && s.cursor.Y <= bottom {
		for y := bottom; y >= s.cursor.Y; y-- {
			if y+count <= bottom {
				if src, ok := s.buffer[y]; ok {
					s.buffer[y+count] = src
				}
			}
			delete(s.buffer, y)
		}
		s.CarriageReturn()
	}
}

// DeleteLines deletes count lines starting at the cursor line; lines
// below move up. A noop when the cursor is outside the scrolling region.
//
// Quirk preserved from pyte: a destination line is only replaced when the
// source line is explicitly stored in the sparse buffer, so deleting into
// never-touched lines leaves the destination content in place.
func (s *Screen) DeleteLines(count int) {
	if count == 0 {
		count = 1
	}
	top, bottom := s.marginsOrScreen()

	if top <= s.cursor.Y && s.cursor.Y <= bottom {
		for y := s.cursor.Y; y <= bottom; y++ {
			if y+count <= bottom {
				if src, ok := s.buffer[y+count]; ok {
					delete(s.buffer, y+count)
					s.buffer[y] = src
				}
			} else {
				delete(s.buffer, y)
			}
		}
		s.CarriageReturn()
	}
}

// InsertCharacters inserts count blank characters at the cursor position,
// shifting the rest of the line right; the cursor does not move.
func (s *Screen) InsertCharacters(count int) {
	if count == 0 {
		count = 1
	}
	line := s.line(s.cursor.Y)
	def := s.defaultChar()
	for x := s.columns; x >= s.cursor.X; x-- {
		if x+count <= s.columns {
			line[x+count] = cellAt(line, x, def)
		}
		delete(line, x)
	}
}

// DeleteCharacters deletes count characters starting at the cursor
// position; characters to the right move left with their attributes.
func (s *Screen) DeleteCharacters(count int) {
	if count == 0 {
		count = 1
	}
	line := s.line(s.cursor.Y)
	for x := s.cursor.X; x < s.columns; x++ {
		if x+count <= s.columns {
			if src, ok := line[x+count]; ok {
				delete(line, x+count)
				line[x] = src
			} else {
				line[x] = s.defaultChar()
			}
		} else {
			delete(line, x)
		}
	}
}

// EraseCharacters erases count characters starting at the cursor
// position, setting them to the cursor attributes; the cursor stays put.
func (s *Screen) EraseCharacters(count int) {
	if count == 0 {
		count = 1
	}
	line := s.line(s.cursor.Y)
	for x := s.cursor.X; x < min(s.cursor.X+count, s.columns); x++ {
		line[x] = s.cursor.Attrs
	}
}

// EraseInLine erases the current line: 0 from the cursor to the end, 1
// from the beginning to the cursor, 2 the complete line. Erased cells take
// the cursor attributes. The private flag is accepted for stream
// compatibility and ignored, as in pyte.
func (s *Screen) EraseInLine(how int, private bool) {
	_ = private
	var start, end int
	switch how {
	case 0:
		start, end = s.cursor.X, s.columns
	case 1:
		start, end = 0, s.cursor.X+1
	case 2:
		start, end = 0, s.columns
	default:
		return
	}

	line := s.line(s.cursor.Y)
	for x := start; x < end; x++ {
		line[x] = s.cursor.Attrs
	}
}

// EraseInDisplay erases the screen: 0 from the cursor to the end, 1 from
// the beginning to the cursor, 2 or 3 the whole display (the cursor does
// not move).
//
// Quirk preserved from pyte: on full lines only explicitly stored cells
// are rewritten with the cursor attributes; unset cells keep rendering as
// the default character.
func (s *Screen) EraseInDisplay(how int) {
	var start, end int
	switch how {
	case 0:
		start, end = s.cursor.Y+1, s.lines
	case 1:
		start, end = 0, s.cursor.Y
	case 2, 3:
		start, end = 0, s.lines
	default:
		return
	}

	for y := start; y < end; y++ {
		line := s.line(y)
		for x := range line {
			line[x] = s.cursor.Attrs
		}
	}

	if how == 0 || how == 1 {
		s.EraseInLine(how, false)
	}
}
