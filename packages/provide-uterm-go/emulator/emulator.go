//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package emulator provides the VT/ANSI terminal emulator used by transport
// sessions, backed by the vt package (the pyte port). Port of
// provide.uterm.emulator.
package emulator

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"time"
	"unicode"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/render"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/screen"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/session"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/vt"
)

// rawTailMax bounds the rolling raw-output tail (decoded chars, ANSI/control
// intact). The vt Screen keeps only the visible viewport, so output that
// scrolls off within a single server turn is otherwise unrecoverable; the
// tail lets consumers recover it. Kept small so it rides every snapshot
// frame cheaply.
const rawTailMax = 4096

// TerminalEmulator is a VT/ANSI terminal emulator backed by a vt.Screen.
//
// Memory bounds: only the visible viewport (cols × rows cells) is retained —
// scrolling overwrites rather than buffering. Applications that need history
// must record the raw byte stream separately (see the replay package).
type TerminalEmulator struct {
	cols, rows int
	term       string
	encoding   Encoding
	// utf8 is the decoder for the whole stream in a UTF-8 session: a
	// transport hands over whatever its last read returned, which can end
	// inside a multibyte character.
	utf8    screen.UTF8Decoder
	screen  *vt.Screen
	stream  *vt.Stream
	dirty   bool
	last    *session.Snapshot
	rawTail string
}

// New creates a CP437 emulator; cols/rows <= 0 select 80×25, empty term
// selects "ANSI".
func New(cols, rows int, term string) *TerminalEmulator {
	return NewWithEncoding(cols, rows, term, CP437)
}

// NewWithEncoding creates an emulator that decodes what it is fed with
// encoding, as New does otherwise.
func NewWithEncoding(cols, rows int, term string, encoding Encoding) *TerminalEmulator {
	if cols <= 0 {
		cols = 80
	}
	if rows <= 0 {
		rows = 25
	}
	if term == "" {
		term = "ANSI"
	}
	s := vt.NewScreen(cols, rows)
	stream := vt.NewStream(s)
	// A UTF-8 session honours DEC special graphics (ESC ( 0, SO/SI), as the
	// reference's does: its decoded text no longer needs the stream's UTF-8
	// mode. A CP437 session keeps the default, so a stray SO in ANSI art
	// stays inert.
	stream.UseUTF8 = encoding != UTF8
	return &TerminalEmulator{
		cols:     cols,
		rows:     rows,
		term:     term,
		encoding: encoding,
		screen:   s,
		stream:   stream,
		dirty:    true,
	}
}

// Process feeds raw bytes through the emulator, decoded with its encoding,
// retaining a bounded tail of the decoded stream (ANSI/control intact).
func (e *TerminalEmulator) Process(data []byte) {
	var text string
	if e.encoding == UTF8 {
		text = e.utf8.Decode(data)
	} else {
		text = screen.DecodeCP437(data)
	}
	e.stream.Feed(text)
	if text != "" {
		combined := e.rawTail + text
		if len(combined) > rawTailMax {
			// Trim to the byte budget without splitting a rune.
			cut := len(combined) - rawTailMax
			for cut < len(combined) && !isRuneStart(combined[cut]) {
				cut++
			}
			combined = combined[cut:]
		}
		e.rawTail = combined
	}
	e.dirty = true
}

func isRuneStart(b byte) bool { return b&0xC0 != 0x80 }

// RawTail returns the bounded rolling tail of raw decoded output (ANSI
// intact).
func (e *TerminalEmulator) RawTail() string {
	return e.rawTail
}

// isCursorAtEnd applies the BBS-prompt heuristic: prompts often leave 1-2
// trailing spaces after the input caret, so the cursor counts as "at end"
// from two columns before the end of the last content line. A tighter check
// misclassified TradeWars 2002 and Major BBS prompts (see the Python
// emulator's history note).
func (e *TerminalEmulator) isCursorAtEnd() bool {
	cursor := e.screen.Cursor()
	lines := e.screen.Display()
	for rowIdx := len(lines) - 1; rowIdx >= 0; rowIdx-- {
		line := strings.TrimRightFunc(lines[rowIdx], unicode.IsSpace)
		if line == "" {
			continue
		}
		if cursor.Y == rowIdx {
			return cursor.X >= len([]rune(line))-2
		}
		return cursor.Y > rowIdx
	}
	return true
}

// GetSnapshot returns the current screen state. The heavy fields are cached
// until the next Process/Reset/Resize; CapturedAt is always fresh.
func (e *TerminalEmulator) GetSnapshot() session.Snapshot {
	if e.last == nil || e.dirty {
		screenText := strings.Join(e.screen.Display(), "\n")
		digest := sha256.Sum256([]byte(screenText))
		cursor := e.screen.Cursor()
		snap := session.Snapshot{
			Screen:      screenText,
			ScreenHash:  hex.EncodeToString(digest[:]),
			Cursor:      session.Cursor{X: cursor.X, Y: cursor.Y},
			Cols:        e.cols,
			Rows:        e.rows,
			Term:        e.term,
			CursorAtEnd: e.isCursorAtEnd(),
			// Mirrors Python's rstrip() != rstrip(" :") check — true when
			// the visible text ends with a colon (optionally space-padded)
			// or a trailing blank line.
			HasTrailingSpace: strings.TrimRightFunc(screenText, unicode.IsSpace) != strings.TrimRight(screenText, " :"),
			RawTail:          e.rawTail,
		}
		e.last = &snap
		e.dirty = false
	}
	out := *e.last
	out.CapturedAt = float64(time.Now().UnixNano()) / 1e9
	return out
}

// ANSIScreen returns the current screen as a single string with ANSI SGR
// codes: per-cell styles are re-emitted whenever they change, each row ends
// with a reset, rows join with newlines. Use this when a live renderer needs
// the visual state including colors — GetSnapshot's Screen field is plain
// text.
//
// It is a snapshot a viewer resumes the live stream from, so it also carries
// the state that stream assumes and the cells do not show: the alternate
// screen before the rows, and after them the scroll region, origin mode, the
// real cursor (deferred wrap included), autowrap/insert/new-line modes, input
// modes, the G0/G1 sets and shift where the stream honours them (a UTF-8
// session; a CP437 one ignores them, so has none to restore), the pen and
// cursor visibility.
func (e *TerminalEmulator) ANSIScreen() string {
	rows := strings.Join(render.RenderScreenLines(e.screen, e.cols, e.rows), "\n")
	return render.ScreenStatePrefix(e.screen) + rows + render.ScreenStateSuffix(e.screen, !e.stream.UseUTF8)
}

// SetScrollbackSink hands sink each row that scrolls off the top of the main
// screen, as plain text. This emulator keeps only the visible screen; whoever
// wants the history a terminal would let its user scroll back through keeps
// it, from these rows. nil stops it; a Reset leaves it in place. Port of
// TerminalEmulator.set_scrollback_sink.
func (e *TerminalEmulator) SetScrollbackSink(sink func(line string)) {
	e.screen.ScrolledOff = sink
}

// Screen exposes the backing vt.Screen.
func (e *TerminalEmulator) Screen() *vt.Screen {
	return e.screen
}

// Cols returns the terminal width.
func (e *TerminalEmulator) Cols() int { return e.cols }

// Rows returns the terminal height.
func (e *TerminalEmulator) Rows() int { return e.rows }

// Reset resets the terminal to its initial state.
func (e *TerminalEmulator) Reset() {
	e.screen.Reset()
	e.dirty = true
}

// Resize resizes the terminal (vt.Screen.Resize takes rows, cols — matching
// pyte's (lines, columns) order).
func (e *TerminalEmulator) Resize(cols, rows int) {
	e.cols = cols
	e.rows = rows
	e.screen.Resize(rows, cols)
	e.dirty = true
}
