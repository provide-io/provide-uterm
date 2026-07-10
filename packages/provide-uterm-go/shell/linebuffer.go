//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import "strings"

// Control bytes handled specially by LineBuffer.
const (
	ctrlC = '\x03'
	ctrlD = '\x04'
)

// DefaultMaxLine is the default per-line character cap (matches _repl.py).
const DefaultMaxLine = 4096

// LineBuffer is a stateful line editor that accumulates raw keystroke bytes
// from xterm.js into complete lines. It is a faithful port of
// provide.uterm.shell._repl.LineBuffer.
//
// Callers feed raw data with Feed and then drain echo (TakeEcho) and completed
// lines (TakeCompleted) separately. It operates on Unicode code points, so a
// multi-byte keystroke counts as one character, exactly like the Python str.
//
// Protocol notes:
//   - Enter → \r (CR); \r\n collapses to a single submit.
//   - Backspace → \x7f (DEL) or \x08 (BS).
//   - Ctrl+C → \x03 (clears the line and completes as "\x03").
//   - Ctrl+D → \x04 (empty-submit; completes the buffer, or "\x04" if empty).
//   - ESC-introduced VT sequences (CSI \x1b[ …, SS3 \x1bO …) are swallowed.
type LineBuffer struct {
	maxLine   int
	buf       []rune
	echo      strings.Builder
	completed []string
}

// NewLineBuffer returns a LineBuffer with the default max line length.
func NewLineBuffer() *LineBuffer {
	return NewLineBufferMax(DefaultMaxLine)
}

// NewLineBufferMax returns a LineBuffer capping accepted printable characters
// per line at maxLine.
func NewLineBufferMax(maxLine int) *LineBuffer {
	return &LineBuffer{maxLine: maxLine}
}

// Feed processes data (raw keystroke bytes) and updates internal state.
func (b *LineBuffer) Feed(data string) {
	r := []rune(data)
	i := 0
	for i < len(r) {
		ch := r[i]
		switch {
		case ch == '\r' || ch == '\n':
			// Skip LF immediately following CR (handles \r\n sequences).
			if ch == '\r' && i+1 < len(r) && r[i+1] == '\n' {
				i++
			}
			b.echo.WriteString("\r\n")
			b.completed = append(b.completed, string(b.buf))
			b.buf = b.buf[:0]
			i++

		case ch == '\x7f' || ch == '\x08': // DEL or BS — backspace
			if len(b.buf) > 0 {
				b.buf = b.buf[:len(b.buf)-1]
				b.echo.WriteString("\x08 \x08")
			}
			i++

		case ch == ctrlC:
			b.buf = b.buf[:0]
			b.echo.WriteString("^C\r\n")
			b.completed = append(b.completed, string(ctrlC))
			i++

		case ch == ctrlD:
			// Treat like Enter with empty line — lets caller handle EOF.
			b.echo.WriteString("\r\n")
			if len(b.buf) > 0 {
				b.completed = append(b.completed, string(b.buf))
			} else {
				b.completed = append(b.completed, string(ctrlD))
			}
			b.buf = b.buf[:0]
			i++

		case ch == '\x1b':
			// VT escape sequence — swallow entirely (arrow keys, F-keys, etc.).
			i = consumeEscape(r, i)

		case ch >= ' ' || ch == '\t':
			// Printable character (or tab — shown as-is).
			if len(b.buf) < b.maxLine {
				b.buf = append(b.buf, ch)
				b.echo.WriteRune(ch)
			}
			i++

		default:
			// Other control bytes — ignore silently.
			i++
		}
	}
}

// TakeEcho returns the accumulated echo string and clears it.
func (b *LineBuffer) TakeEcho() string {
	s := b.echo.String()
	b.echo.Reset()
	return s
}

// TakeCompleted returns the completed lines and clears the internal list.
func (b *LineBuffer) TakeCompleted() []string {
	lines := b.completed
	b.completed = nil
	return lines
}

// CurrentLine returns the current (uncommitted) line buffer contents.
func (b *LineBuffer) CurrentLine() string {
	return string(b.buf)
}

// Clear discards the current line buffer and pending echo without emitting a
// completed line.
func (b *LineBuffer) Clear() {
	b.buf = b.buf[:0]
	b.echo.Reset()
}

// consumeEscape consumes a VT escape sequence starting at r[i] and returns the
// index of the next character. Port of LineBuffer._consume_escape.
func consumeEscape(r []rune, i int) int {
	j := i + 1
	if j < len(r) && r[j] == '[' {
		j++
		// Consume parameter bytes (0x30-0x3F) and intermediate bytes (0x20-0x2F).
		for j < len(r) && r[j] < 0x40 {
			j++
		}
		// Consume the final byte (0x40-0x7E).
		if j < len(r) && r[j] >= 0x40 && r[j] <= 0x7e {
			j++
		}
	} else if j < len(r) && r[j] == 'O' {
		// SS3 sequences (e.g. F1-F4 on some terminals).
		j++
		if j < len(r) {
			j++
		}
	}
	return j
}
