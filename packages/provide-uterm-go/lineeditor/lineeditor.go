//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package lineeditor provides a generic line editor for terminal input with
// readline-style shortcuts. Port of provide.uterm.line_editor.
//
// It accumulates input characters until Enter, with cursor tracking,
// backspace/delete, Ctrl+A/E/B/F/U/K/W shortcuts, password masking, and a
// configurable maximum line length. Cursor movement uses relative VT100
// sequences so the editor does not need to know the screen column where
// input began.
package lineeditor

import (
	"fmt"
	"strings"
)

// WriteFunc is the terminal-output callback. Errors propagate to the caller
// of ProcessChar (the Python port raises through the awaited callback).
type WriteFunc func(string) error

// LineEditor is a stateful line editor for terminal sessions.
type LineEditor struct {
	// MaxLength is the maximum number of characters to accept.
	MaxLength int
	// PasswordMode masks echoed input with asterisks.
	PasswordMode bool
	// OnWrite, when non-nil, receives all output including echoes and cursor
	// movements. When nil the editor is silent.
	OnWrite WriteFunc

	buffer    []rune
	cursorPos int // index into buffer; 0 = before first rune
}

// New creates a LineEditor. maxLength <= 0 selects the Python default of 80.
func New(maxLength int, passwordMode bool, onWrite WriteFunc) *LineEditor {
	if maxLength <= 0 {
		maxLength = 80
	}
	return &LineEditor{MaxLength: maxLength, PasswordMode: passwordMode, OnWrite: onWrite}
}

func (e *LineEditor) emit(text string) error {
	if e.OnWrite != nil {
		return e.OnWrite(text)
	}
	return nil
}

func (e *LineEditor) display(s []rune) string {
	if e.PasswordMode {
		return strings.Repeat("*", len(s))
	}
	return string(s)
}

// ProcessChar processes a single input rune. When Enter completes a line it
// returns (line, true, nil); otherwise done is false.
func (e *LineEditor) ProcessChar(ch rune) (line string, done bool, err error) {
	switch ch {
	case '\r', '\n':
		result := string(e.buffer)
		e.buffer = nil
		e.cursorPos = 0
		return result, true, e.emit("\r\n")
	case 0x7f, 0x08: // Backspace / Delete
		if e.cursorPos > 0 {
			tail := append([]rune(nil), e.buffer[e.cursorPos:]...)
			e.buffer = append(e.buffer[:e.cursorPos-1], tail...)
			e.cursorPos--
			// Move left 1, redraw tail, overwrite extra char at end, move back.
			seq := "\x08" + e.display(tail) + " " + fmt.Sprintf("\x1b[%dD", len(tail)+1)
			return "", false, e.emit(seq)
		}
		return "", false, nil
	case 0x01: // Ctrl+A: move to beginning of line
		if e.cursorPos > 0 {
			seq := fmt.Sprintf("\x1b[%dD", e.cursorPos)
			e.cursorPos = 0
			return "", false, e.emit(seq)
		}
		return "", false, nil
	case 0x05: // Ctrl+E: move to end of line
		if n := len(e.buffer) - e.cursorPos; n > 0 {
			e.cursorPos = len(e.buffer)
			return "", false, e.emit(fmt.Sprintf("\x1b[%dC", n))
		}
		return "", false, nil
	case 0x02: // Ctrl+B: move left one character
		if e.cursorPos > 0 {
			e.cursorPos--
			return "", false, e.emit("\x1b[D")
		}
		return "", false, nil
	case 0x06: // Ctrl+F: move right one character
		if e.cursorPos < len(e.buffer) {
			e.cursorPos++
			return "", false, e.emit("\x1b[C")
		}
		return "", false, nil
	case 0x15: // Ctrl+U: kill backward (cursor to start of line)
		if e.cursorPos > 0 {
			remaining := append([]rune(nil), e.buffer[e.cursorPos:]...)
			e.buffer = remaining
			seq := fmt.Sprintf("\x1b[%dD", e.cursorPos) // move to start of input
			seq += e.display(remaining)                 // redraw remaining chars
			seq += "\x1b[K"                             // erase from here to EOL
			if len(remaining) > 0 {
				seq += fmt.Sprintf("\x1b[%dD", len(remaining)) // cursor back to start
			}
			e.cursorPos = 0
			return "", false, e.emit(seq)
		}
		return "", false, nil
	case 0x0b: // Ctrl+K: kill forward (cursor to end of line)
		if e.cursorPos < len(e.buffer) {
			e.buffer = e.buffer[:e.cursorPos]
			return "", false, e.emit("\x1b[K")
		}
		return "", false, nil
	case 0x17: // Ctrl+W: kill word backward
		if e.cursorPos > 0 {
			pos := e.cursorPos
			for pos > 0 && e.buffer[pos-1] == ' ' {
				pos--
			}
			for pos > 0 && e.buffer[pos-1] != ' ' {
				pos--
			}
			deleted := e.cursorPos - pos
			remaining := append([]rune(nil), e.buffer[e.cursorPos:]...)
			e.buffer = append(e.buffer[:pos], remaining...)
			seq := fmt.Sprintf("\x1b[%dD", deleted)
			seq += e.display(remaining)
			seq += "\x1b[K"
			if len(remaining) > 0 {
				seq += fmt.Sprintf("\x1b[%dD", len(remaining))
			}
			e.cursorPos = pos
			return "", false, e.emit(seq)
		}
		return "", false, nil
	}

	// Regular character insertion.
	if len(e.buffer) >= e.MaxLength {
		return "", false, e.emit("\a")
	}
	tail := append([]rune(nil), e.buffer[e.cursorPos:]...)
	e.buffer = append(append(e.buffer[:e.cursorPos], ch), tail...)
	e.cursorPos++
	if len(tail) == 0 {
		// Inserting at end: simple echo.
		if e.PasswordMode {
			return "", false, e.emit("*")
		}
		return "", false, e.emit(string(ch))
	}
	// Mid-line insert: echo new char + redraw tail, move cursor back.
	seq := e.display(append([]rune{ch}, tail...)) + fmt.Sprintf("\x1b[%dD", len(tail))
	return "", false, e.emit(seq)
}

// Reset clears the buffer and cursor.
func (e *LineEditor) Reset() {
	e.buffer = nil
	e.cursorPos = 0
}

// Buffer returns the current buffer contents.
func (e *LineEditor) Buffer() string {
	return string(e.buffer)
}
