//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"strings"
	"unicode/utf8"
)

// ANSI escape constants. Ported verbatim from _output.py. All multi-line
// strings use CRLF line endings (terminal protocol).
const (
	Reset   = "\x1b[0m"
	Bold    = "\x1b[1m"
	Dim     = "\x1b[2m"
	Green   = "\x1b[32m"
	Yellow  = "\x1b[33m"
	Red     = "\x1b[31m"
	Cyan    = "\x1b[36m"
	Blue    = "\x1b[34m"
	Magenta = "\x1b[35m"

	// ClearScreen erases the screen and homes the cursor.
	ClearScreen = "\x1b[2J\x1b[H"
)

// Prompt is the ushell command prompt.
var Prompt = Green + "❯" + Reset + " "

// Banner is the ushell startup banner.
var Banner = Bold + Cyan + "ushell" + Reset + " " + Dim + "— Python REPL inside your terminal" + Reset + "\r\n" +
	Dim + "Type " + Reset + "help" + Dim + " for available commands." + Reset + "\r\n\r\n"

// ErrorMsg formats a red "error:" line.
func ErrorMsg(text string) string {
	return Red + "error:" + Reset + " " + text + "\r\n"
}

// InfoMsg formats a dim informational line.
func InfoMsg(text string) string {
	return Dim + text + Reset + "\r\n"
}

// SuccessMsg formats a green success line.
func SuccessMsg(text string) string {
	return Green + text + Reset + "\r\n"
}

// Heading formats a bold-cyan heading line.
func Heading(text string) string {
	return Bold + Cyan + text + Reset + "\r\n"
}

// FmtKV formats a key/value pair with the key left-padded to width columns.
// Mirrors _output.fmt_kv (default width 20).
func FmtKV(key, value string, width int) string {
	return "  " + Dim + padRight(key, width) + Reset + value + "\r\n"
}

// FmtKVDefault is FmtKV with the default width of 20.
func FmtKVDefault(key, value string) string {
	return FmtKV(key, value, 20)
}

// FmtTable formats rows as a fixed-width table. When headers is non-nil a bold
// header row plus a dashed separator precede the data. Mirrors
// _output.fmt_table.
func FmtTable(rows [][]string, headers []string) string {
	if len(rows) == 0 {
		return InfoMsg("(no results)")
	}
	// Per-column max width over the data cells.
	ncols := 0
	for _, r := range rows {
		if len(r) > ncols {
			ncols = len(r)
		}
	}
	widths := make([]int, ncols)
	for _, r := range rows {
		for i, cell := range r {
			if w := utf8.RuneCountInString(cell); w > widths[i] {
				widths[i] = w
			}
		}
	}
	if headers != nil {
		for i := 0; i < len(headers) && i < ncols; i++ {
			if w := utf8.RuneCountInString(headers[i]); w > widths[i] {
				widths[i] = w
			}
		}
	}

	var lines []string
	if headers != nil {
		cells := make([]string, 0, len(headers))
		for i, h := range headers {
			w := 0
			if i < len(widths) {
				w = widths[i]
			}
			cells = append(cells, Bold+padRight(h, w)+Reset)
		}
		lines = append(lines, "  "+strings.Join(cells, "  "))
		dashes := make([]string, len(widths))
		for i, w := range widths {
			dashes[i] = strings.Repeat("-", w)
		}
		lines = append(lines, "  "+strings.Join(dashes, "  "))
	}
	for _, r := range rows {
		cells := make([]string, len(r))
		for i, cell := range r {
			w := 0
			if i < len(widths) {
				w = widths[i]
			}
			cells[i] = padRight(cell, w)
		}
		lines = append(lines, "  "+strings.Join(cells, "  "))
	}
	return strings.Join(lines, "\r\n") + "\r\n"
}

// padRight left-justifies s to w display columns (rune count), matching
// Python's f"{s:<{w}}" for the ASCII cells used here.
func padRight(s string, w int) string {
	n := utf8.RuneCountInString(s)
	if n >= w {
		return s
	}
	return s + strings.Repeat(" ", w-n)
}
