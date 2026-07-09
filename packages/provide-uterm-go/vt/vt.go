//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package vt is a Go port of the pyte in-memory VT102/Linux terminal
// emulator (Screen + Stream). It reproduces pyte's exact semantics —
// including its quirks — so that output rendered through this package
// matches what pyte.Screen/pyte.Stream produce for the same input.
//
// The two entry points are:
//
//	screen := vt.NewScreen(80, 24)
//	stream := vt.NewStream(screen)
//	stream.Feed("\x1b[1;31mhello\r\n")
//	lines := screen.Display()
//
// Stream accepts already-decoded text (Go strings interpreted as a
// sequence of Unicode code points), mirroring pyte.Stream (the str-based
// stream, not ByteStream). Charset selection sequences (SO/SI, ESC ( /
// ESC )) are ignored while UseUTF8 is true, exactly like pyte.
package vt

// Char is a single styled on-screen character, mirroring pyte.screens.Char.
//
// Data holds the character cell contents: normally a single rune, but it
// may be empty (the stub cell following a full-width character) or contain
// trailing combining characters merged by NFC normalization.
//
// FG and BG are color names ("default", "red", ... , "brightred", ...) or
// 6+ hex digit strings for 256/true color. Dim exists for API completeness
// but is never set by SGR handling because pyte has no dim attribute.
type Char struct {
	Data          string
	FG, BG        string
	Bold          bool
	Dim           bool
	Italics       bool
	Underscore    bool
	Strikethrough bool
	Reverse       bool
	Blink         bool
}

// defaultCharPlain is pyte's Char(" ") — a space with default attributes.
var defaultCharPlain = Char{Data: " ", FG: "default", BG: "default"}

// Cursor is the screen cursor: 0-based position, the attributes applied to
// newly drawn characters, and visibility (DECTCEM).
type Cursor struct {
	X, Y   int
	Attrs  Char
	Hidden bool
}

// Margins is the scrolling region selected by DECSTBM, as 0-based
// inclusive top and bottom line indices.
type Margins struct {
	Top, Bottom int
}

// savepoint captures cursor state on DECSC, mirroring pyte's Savepoint.
type savepoint struct {
	cursor  Cursor
	g0, g1  *charsetMap
	charset int
	origin  bool
	wrap    bool
}

// charsetMap translates code points below 256, mirroring Python's
// str.translate over pyte's 256-entry charset strings.
type charsetMap [256]rune

// widthRange is a codepoint range carrying a small integer value, used by
// the generated wcwidth and combining-class tables.
type widthRange struct {
	lo, hi rune
	val    int
}

// compKey is a canonical composition pair (starter, combiner).
type compKey struct {
	first, second rune
}

// withData returns c with its Data replaced, mirroring Char._replace(data=...).
func withData(c Char, data string) Char {
	c.Data = data
	return c
}

// cellAt returns the cell stored at x, or def when the cell is unset —
// mirroring pyte's StaticDefaultDict lookup.
func cellAt(line map[int]Char, x int, def Char) Char {
	if c, ok := line[x]; ok {
		return c
	}
	return def
}
