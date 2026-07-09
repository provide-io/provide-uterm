//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

import "strings"

// Control characters recognized by the stream (pyte.control).
const (
	ctrlNUL   = 0x00
	ctrlBEL   = 0x07
	ctrlBS    = 0x08
	ctrlHT    = 0x09
	ctrlLF    = 0x0a
	ctrlVT    = 0x0b
	ctrlFF    = 0x0c
	ctrlCR    = 0x0d
	ctrlSO    = 0x0e
	ctrlSI    = 0x0f
	ctrlCAN   = 0x18
	ctrlSUB   = 0x1a
	ctrlESC   = 0x1b
	ctrlDEL   = 0x7f
	ctrlCSIC1 = 0x9b
	ctrlSTC1  = 0x9c
	ctrlOSCC1 = 0x9d
)

// Parser states.
const (
	stGround = iota
	stEscape
	stSharp
	stPercent
	stCharset
	stCSI
	stCSIDollar
	stOSCCode
	stOSCParam
)

// Stream is a state machine that parses a stream of terminal input and
// dispatches events to a Screen, mirroring pyte.streams.Stream (the
// text-based stream).
type Stream struct {
	// UseUTF8 mirrors pyte.Stream.use_utf8 (default true): while set,
	// SO/SI and ESC ( / ESC ) charset selection are ignored.
	UseUTF8 bool

	screen          *Screen
	takingPlainText bool
	state           int

	// CSI collection state.
	csiParams  []int
	csiCurrent int
	csiPrivate bool

	// OSC collection state.
	oscCode  rune
	oscParam strings.Builder
	oscESC   bool

	// Charset designation state (ESC ( or ESC )).
	charsetMode rune
}

// NewStream creates a stream dispatching to the given screen.
func NewStream(screen *Screen) *Stream {
	return &Stream{
		UseUTF8:         true,
		screen:          screen,
		takingPlainText: true,
	}
}

// isSpecial reports whether r must go through the state machine rather
// than being drawn as part of a plain-text run.
func isSpecial(r rune) bool {
	return r == ctrlNUL || (r >= ctrlBEL && r <= ctrlSI) ||
		r == ctrlESC || r == ctrlDEL || r == ctrlCSIC1 || r == ctrlOSCC1
}

// Feed consumes a chunk of input, advancing parser state as necessary.
// Maximal runs of plain text are dispatched to Screen.Draw as single
// batches, exactly like pyte's regex-based fast path — batch boundaries
// are observable because Draw stops at unprintable characters.
func (st *Stream) Feed(data string) {
	runes := []rune(data)
	length := len(runes)
	offset := 0
	for offset < length {
		if st.takingPlainText {
			end := offset
			for end < length && !isSpecial(runes[end]) {
				end++
			}
			if end > offset {
				st.screen.Draw(string(runes[offset:end]))
				offset = end
			} else {
				st.takingPlainText = false
			}
		} else {
			st.takingPlainText = st.send(runes[offset])
			offset++
		}
	}
}

// send advances the state machine by one rune, reporting whether the
// parser is back at ground (allowing the plain-text fast path).
func (st *Stream) send(r rune) bool {
	switch st.state {
	case stGround:
		st.ground(r)
	case stEscape:
		st.escape(r)
	case stSharp:
		if r == '8' {
			st.screen.AlignmentDisplay()
		}
		st.state = stGround
	case stPercent:
		// ESC % — select-other-charset is a noop for text streams.
		st.state = stGround
	case stCharset:
		if !st.UseUTF8 {
			st.screen.DefineCharset(string(r), string(st.charsetMode))
		}
		st.state = stGround
	case stCSI:
		st.csi(r)
	case stCSIDollar:
		// XTerm ESC ] ... $ [a-z] sequences: skip one char and bail.
		st.state = stGround
	case stOSCCode:
		st.oscBegin(r)
	case stOSCParam:
		st.osc(r)
	}
	return st.state == stGround
}

// ground handles a special character in the ground state.
func (st *Stream) ground(r rune) {
	switch {
	case r == ctrlESC:
		st.state = stEscape
	case isBasic(r):
		// Shifts are ignored in UTF-8 mode.
		if (r == ctrlSI || r == ctrlSO) && st.UseUTF8 {
			return
		}
		st.dispatchBasic(r)
	case r == ctrlCSIC1:
		st.enterCSI()
	case r == ctrlOSCC1:
		st.state = stOSCCode
	case r == ctrlNUL || r == ctrlDEL:
		// Ignored.
	default:
		st.screen.Draw(string(r))
	}
}

// escape handles the character following ESC.
func (st *Stream) escape(r rune) {
	switch r {
	case '[':
		st.enterCSI()
	case ']':
		st.state = stOSCCode
	case '#':
		st.state = stSharp
	case '%':
		st.state = stPercent
	case '(', ')':
		st.charsetMode = r
		st.state = stCharset
	default:
		st.dispatchEscape(r)
		st.state = stGround
	}
}

// enterCSI resets CSI collection state and enters the CSI state.
func (st *Stream) enterCSI() {
	st.csiParams = st.csiParams[:0]
	st.csiCurrent = 0
	st.csiPrivate = false
	st.state = stCSI
}

// csi handles one character of a control sequence.
func (st *Stream) csi(r rune) {
	switch {
	case r == '?':
		st.csiPrivate = true
	case r == ctrlBEL || r == ctrlBS || r == ctrlHT || r == ctrlLF ||
		r == ctrlVT || r == ctrlFF || r == ctrlCR:
		// Control characters allowed (and executed) inside CSI.
		st.dispatchBasic(r)
	case r == ' ' || r == '>':
		// Secondary DA is not supported.
	case r == ctrlCAN || r == ctrlSUB:
		// CAN/SUB abort the sequence; the substitute character is drawn.
		st.screen.Draw(string(r))
		st.state = stGround
	case r >= '0' && r <= '9':
		if st.csiCurrent < 100000 { // Parameters saturate at 9999 anyway.
			st.csiCurrent = st.csiCurrent*10 + int(r-'0')
		}
	case r == '$':
		// XTerm-specific sequences are not supported: skip one char.
		st.state = stCSIDollar
	default:
		st.csiParams = append(st.csiParams, min(st.csiCurrent, 9999))
		if r == ';' {
			st.csiCurrent = 0
		} else {
			st.dispatchCSI(r, st.csiParams, st.csiPrivate)
			st.state = stGround
		}
	}
}

// oscBegin handles the character after ESC ] — the OSC command code.
func (st *Stream) oscBegin(r rune) {
	if r == 'R' || r == 'P' {
		// Reset/set palette: not implemented.
		st.state = stGround
		return
	}
	st.oscCode = r
	st.oscParam.Reset()
	st.oscESC = false
	st.state = stOSCParam
}

// osc accumulates the OSC parameter until a terminator (BEL, ST).
func (st *Stream) osc(r rune) {
	if st.oscESC {
		st.oscESC = false
		if r == '\\' { // ESC \ is the C0 string terminator.
			st.oscDispatch()
			return
		}
		st.oscParam.WriteRune(ctrlESC)
		st.oscParam.WriteRune(r)
		return
	}
	switch r {
	case ctrlESC:
		st.oscESC = true
	case ctrlSTC1, ctrlBEL:
		st.oscDispatch()
	default:
		st.oscParam.WriteRune(r)
	}
}

// oscDispatch fires the collected OSC command and returns to ground.
func (st *Stream) oscDispatch() {
	param := st.oscParam.String()
	// Drop the leading ";" separator (pyte drops the first code point
	// unconditionally).
	if _, size := firstRune(param); size > 0 {
		param = param[size:]
	}
	if st.oscCode == '0' || st.oscCode == '1' {
		st.screen.SetIconName(param)
	}
	if st.oscCode == '0' || st.oscCode == '2' {
		st.screen.SetTitle(param)
	}
	st.state = stGround
}

// firstRune returns the first rune of s and its encoded size (0 if empty).
func firstRune(s string) (rune, int) {
	for _, r := range s {
		return r, len(string(r))
	}
	return 0, 0
}

// isBasic reports whether r is a basic control character with a direct
// screen handler.
func isBasic(r rune) bool {
	return r >= ctrlBEL && r <= ctrlSI
}

// dispatchBasic executes a basic control character.
func (st *Stream) dispatchBasic(r rune) {
	switch r {
	case ctrlBEL:
		st.screen.Bell()
	case ctrlBS:
		st.screen.Backspace()
	case ctrlHT:
		st.screen.Tab()
	case ctrlLF, ctrlVT, ctrlFF:
		st.screen.LineFeed()
	case ctrlCR:
		st.screen.CarriageReturn()
	case ctrlSO:
		st.screen.ShiftOut()
	case ctrlSI:
		st.screen.ShiftIn()
	}
}

// dispatchEscape executes a non-CSI escape sequence; unknown sequences
// are ignored (pyte routes them to a debug noop).
func (st *Stream) dispatchEscape(r rune) {
	switch r {
	case 'c': // RIS.
		st.screen.Reset()
	case 'D': // IND.
		st.screen.Index()
	case 'E': // NEL.
		st.screen.LineFeed()
	case 'H': // HTS.
		st.screen.SetTabStop()
	case 'M': // RI.
		st.screen.ReverseIndex()
	case '7': // DECSC.
		st.screen.SaveCursor()
	case '8': // DECRC.
		st.screen.RestoreCursor()
	}
}

// dispatchCSI executes a CSI sequence; unknown final characters are
// ignored (pyte routes them to a debug noop). Surplus parameters are
// discarded, missing ones default to 0.
func (st *Stream) dispatchCSI(final rune, params []int, private bool) {
	p := func(i int) int {
		if i < len(params) {
			return params[i]
		}
		return 0
	}
	scr := st.screen
	switch final {
	case '@': // ICH.
		scr.InsertCharacters(p(0))
	case 'A': // CUU.
		scr.CursorUp(p(0))
	case 'B', 'e': // CUD, VPR.
		scr.CursorDown(p(0))
	case 'C', 'a': // CUF, HPR.
		scr.CursorForward(p(0))
	case 'D': // CUB.
		scr.CursorBack(p(0))
	case 'E': // CNL.
		scr.CursorDown1(p(0))
	case 'F': // CPL.
		scr.CursorUp1(p(0))
	case 'G', '\'': // CHA, HPA.
		scr.CursorToColumn(p(0))
	case 'H', 'f': // CUP, HVP.
		scr.CursorPosition(p(0), p(1))
	case 'J': // ED.
		scr.EraseInDisplay(p(0))
	case 'K': // EL.
		scr.EraseInLine(p(0), private)
	case 'L': // IL.
		scr.InsertLines(p(0))
	case 'M': // DL.
		scr.DeleteLines(p(0))
	case 'P': // DCH.
		scr.DeleteCharacters(p(0))
	case 'X': // ECH.
		scr.EraseCharacters(p(0))
	case 'c': // DA.
		scr.ReportDeviceAttributes(p(0), private)
	case 'd': // VPA.
		scr.CursorToLine(p(0))
	case 'g': // TBC.
		scr.ClearTabStop(p(0))
	case 'h': // SM.
		scr.SetMode(private, params...)
	case 'l': // RM.
		scr.ResetMode(private, params...)
	case 'm': // SGR.
		scr.SelectGraphicRendition(params...)
	case 'n': // DSR.
		scr.ReportDeviceStatus(p(0))
	case 'r': // DECSTBM.
		scr.SetMargins(params...)
	}
}
