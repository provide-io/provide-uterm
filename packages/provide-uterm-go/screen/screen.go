//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package screen provides generic screen parsing utilities for BBS terminals.
//
// It is a behavior-faithful port of the Python module
// provide.uterm.screen (packages/provide-uterm/src/provide/uterm/screen.py):
// ANSI stripping, bare-SGR-fragment cleanup, CP437 encode/decode, and
// screen-scraping helpers (action tags, menus, numbered lists, key/value
// extraction).
package screen

import (
	"fmt"
	"regexp"
	"strings"
)

// The escape grammar StripANSI removes, in named parts (screen.py _ESC,
// _CSI_INTRODUCER, ...) so the chunk-carry check (SplitIncompleteEscape) is
// built from the SAME pieces and cannot drift:
//
//	CSI: ESC "[" <parameter bytes 0x30-0x3F>* <intermediate bytes 0x20-0x2F>* <final 0x40-0x7E>
//	Fe:  ESC <one byte 0x40-0x5F> (a two-byte escape; "[" itself is one, so a
//	     bare "ESC [" with nothing after it is ambiguous until more arrives)
const (
	escByte              = "\x1b"
	csiIntroducer        = `\[`
	csiParameterBytes    = `[0-?]*`
	csiIntermediateBytes = `[ -/]*`
	csiFinalByte         = `[@-~]`
	feFinalByte          = `[@-_]`
)

// csiPrefix is _CSI_PREFIX: a CSI up to, not including, its final byte.
var csiPrefix = strings.Join([]string{csiIntroducer, csiParameterBytes, csiIntermediateBytes}, "")

// ansiEscapeRE mirrors Python's _ANSI_ESCAPE_RE,
// \x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_]). It has no lookarounds, so it compiles
// under RE2 unchanged.
var ansiEscapeRE = regexp.MustCompile(fmt.Sprintf("%s(?:%s%s|%s)", escByte, csiPrefix, csiFinalByte, feFinalByte))

// incompleteEscapeRE is _INCOMPLETE_ESCAPE_RE, anchored as its fullmatch is: a
// proper prefix of something ansiEscapeRE strips -- a bare ESC, or a CSI whose
// parameter/intermediate bytes have not yet reached a final byte.
var incompleteEscapeRE = regexp.MustCompile(fmt.Sprintf(`\A%s(?:%s)?\z`, escByte, csiPrefix))

// MaxEscapeCarry is MAX_ESCAPE_CARRY: the longest tail SplitIncompleteEscape
// holds back, ESC included. A real sequence is a handful of bytes; past this
// the "sequence" is not one (or is hostile), and it is released into the text
// rather than carried forever.
const MaxEscapeCarry = 64

// SplitIncompleteEscape is split_incomplete_escape: text split into
// (complete, carry), carry being an unterminated trailing escape sequence.
//
// For text that arrives in chunks: StripANSI sees one chunk at a time, so
// "\x1b[1" + "msudo" would strip to "\x1b[1" (no final byte, left alone) and
// "msudo". The caller strips complete now and prepends carry to the next
// chunk, so the whole sequence is stripped once it completes.
//
// carry is non-empty exactly when the text from the LAST ESC to the end is a
// proper prefix of a sequence StripANSI removes and is at most maxCarry long
// (pass MaxEscapeCarry for the reference default). Only the last ESC can start
// one: no byte of an incomplete sequence is ESC. The reference measures the
// bound in code points and this in bytes, which agree on every tail the
// pattern accepts: it accepts ASCII only.
func SplitIncompleteEscape(text string, maxCarry int) (complete, carry string) {
	start := strings.LastIndexByte(text, 0x1b)
	if start < 0 || len(text)-start > maxCarry || !incompleteEscapeRE.MatchString(text[start:]) {
		return text, ""
	}
	return text[:start], text[start:]
}

// actionTagRE mirrors Python's _ACTION_TAG_RE: <([^<>\r\n]{1,80})>
var actionTagRE = regexp.MustCompile(`<([^<>\r\n]{1,80})>`)

// screenPadding is the 80-space prefix used by CleanScreenForDisplay.
var screenPadding = strings.Repeat(" ", 80)

// Default limits matching the Python keyword defaults.
const (
	// DefaultMaxActionTags is the Python default for extract_action_tags(max_tags=8).
	DefaultMaxActionTags = 8
	// DefaultMaxScreenLines is the Python default for clean_screen_for_display(max_lines=30).
	DefaultMaxScreenLines = 30
)

// NormalizeTerminalText normalizes terminal text for robust prompt/context parsing.
//
//   - Removes ANSI escape/control sequences.
//   - Removes isolated bare SGR fragments (e.g. "1;31m") seen in some BBS server output.
//   - Normalizes line endings.
//
// Port of normalize_terminal_text.
func NormalizeTerminalText(text string) string {
	if text == "" {
		return ""
	}
	cleaned := strings.ReplaceAll(text, "\r\n", "\n")
	cleaned = strings.ReplaceAll(cleaned, "\r", "\n")
	cleaned = ansiEscapeRE.ReplaceAllString(cleaned, "")
	cleaned = stripBareSGRLinePrefix(cleaned)
	cleaned = stripBareSGR(cleaned)
	return cleaned
}

// StripANSI removes ANSI escape codes from text. Port of strip_ansi.
func StripANSI(text string) string {
	return NormalizeTerminalText(text)
}

// parseBareSGRBody matches Python's `\d{1,3}(?:;\d{1,3})*` followed by a
// literal 'm', anchored at rune index i. It returns the index of the 'm'
// rune and true on a match.
//
// Because 'm' cannot appear inside \d or ';', the only candidate end is the
// first 'm' after the digit/';' run, and Python's backtracking accepts it
// exactly when the run splits on ';' into non-empty groups of at most three
// digits (Unicode Nd, like Python's \d).
func parseBareSGRBody(rs []rune, i int) (int, bool) {
	groupLen := 0
	for j := i; j < len(rs); j++ {
		switch r := rs[j]; {
		case r == ';':
			if groupLen == 0 {
				return 0, false // empty group (leading or doubled ';')
			}
			groupLen = 0
		case isPyDigit(r):
			groupLen++
			if groupLen > 3 {
				return 0, false // group longer than \d{1,3}
			}
		case r == 'm' && groupLen > 0:
			return j, true
		default:
			return 0, false
		}
	}
	return 0, false // ran off the end without an 'm'
}

// stripBareSGRLinePrefix ports _BARE_SGR_LINE_PREFIX_RE:
//
//	(?m)^(?:\d{1,3}(?:;\d{1,3})*)m(?=[A-Z<])
//
// Go's RE2 has no lookahead, so the trailing (?=[A-Z<]) is checked manually.
// The multiline ^ anchor matches at index 0 or immediately after '\n'
// (carriage returns were already normalized away by the caller). As with
// re.sub, anchors are evaluated against the original string and scanning
// resumes after each match.
func stripBareSGRLinePrefix(s string) string {
	rs := []rune(s)
	var b strings.Builder
	b.Grow(len(s))
	for i := 0; i < len(rs); {
		if i == 0 || rs[i-1] == '\n' {
			if m, ok := parseBareSGRBody(rs, i); ok && m+1 < len(rs) && (rs[m+1] == '<' || (rs[m+1] >= 'A' && rs[m+1] <= 'Z')) {
				i = m + 1 // drop the fragment; the lookahead rune is not consumed
				continue
			}
		}
		b.WriteRune(rs[i])
		i++
	}
	return b.String()
}

// stripBareSGR ports _BARE_SGR_RE:
//
//	(?:(?<=^)|(?<=\n)|(?<=\r)|(?<=\s))(?:\d{1,3}(?:;\d{1,3})*)m(?=\x1b|\s|$)
//
// Go's RE2 has neither lookbehind nor lookahead, so both are checked
// manually: the fragment must start at index 0 or right after a whitespace
// rune ('\n' and '\r' are whitespace, so the explicit alternates collapse),
// and must be followed by ESC, whitespace, or end of string. Python's
// non-multiline $ also matches before a trailing '\n', which is subsumed by
// the \s alternative. Anchors are evaluated against the original string.
func stripBareSGR(s string) string {
	rs := []rune(s)
	var b strings.Builder
	b.Grow(len(s))
	for i := 0; i < len(rs); {
		if i == 0 || isPySpace(rs[i-1]) {
			if m, ok := parseBareSGRBody(rs, i); ok && (m+1 >= len(rs) || rs[m+1] == 0x1b || isPySpace(rs[m+1])) {
				i = m + 1 // drop the fragment; the lookahead rune is not consumed
				continue
			}
		}
		b.WriteRune(rs[i])
		i++
	}
	return b.String()
}

// ExtractActionTags extracts angle-bracket action tags like "<Move>" from a
// screen snapshot, deduplicated case-insensitively, keeping first-seen
// casing. Pass DefaultMaxActionTags for the Python default; values below 1
// are clamped to 1 (Python: max(1, int(max_tags))).
//
// Port of extract_action_tags.
func ExtractActionTags(text string, maxTags int) []string {
	out := []string{}
	if text == "" {
		return out
	}
	if maxTags < 1 {
		maxTags = 1
	}
	seen := make(map[string]struct{})
	for _, m := range actionTagRE.FindAllStringSubmatch(text, -1) {
		tag := pyStrip(m[1])
		if tag == "" {
			continue
		}
		key := strings.ToLower(tag)
		if _, dup := seen[key]; dup {
			continue
		}
		seen[key] = struct{}{}
		out = append(out, tag)
		if len(out) >= maxTags {
			break
		}
	}
	return out
}

// CleanScreenForDisplay cleans a screen for display by removing padding
// lines (whitespace-only lines that start with 80 spaces), returning up to
// maxLines lines. Pass DefaultMaxScreenLines for the Python default.
//
// Port of clean_screen_for_display.
func CleanScreenForDisplay(screen string, maxLines int) []string {
	lines := []string{}
	for _, line := range strings.Split(screen, "\n") {
		if pyStrip(line) != "" || !strings.HasPrefix(line, screenPadding) {
			lines = append(lines, line)
			if len(lines) >= maxLines {
				break
			}
		}
	}
	return lines
}
