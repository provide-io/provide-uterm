//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

// Telnet protocol constants (RFC 854 / 855 / 1073 / 1091). These mirror the
// local copies in the Python telnet_transport module.
const (
	iacByte  byte = 255 // IAC — Interpret As Command
	cmdWILL  byte = 251 // WILL — will perform option
	cmdWONT  byte = 252 // WONT — won't perform option
	cmdDO    byte = 253 // DO   — do perform option
	cmdDONT  byte = 254 // DONT — don't perform option
	cmdSB    byte = 250 // SB   — subnegotiation begin
	cmdSE    byte = 240 // SE   — subnegotiation end
	optECHO  byte = 1   // ECHO
	optSGA   byte = 3   // Suppress Go Ahead
	optNAWS  byte = 31  // Negotiate About Window Size
	optTTYPE byte = 24  // Terminal Type
	ttypeIS  byte = 0   // TTYPE IS sub-command
	optBIN   byte = 0   // BINARY transmission
)

// telnetEventKind distinguishes a negotiation event from a subnegotiation.
type telnetEventKind int

const (
	// evNegotiate is a DO/DONT/WILL/WONT command event.
	evNegotiate telnetEventKind = iota
	// evSubneg is an SB..IAC SE subnegotiation payload event.
	evSubneg
)

// telnetEvent is a parsed control event. For evNegotiate, cmd holds the command
// byte and opt the option byte. For evSubneg, payload holds the SB payload.
type telnetEvent struct {
	kind    telnetEventKind
	cmd     byte
	opt     byte
	payload []byte
}

// findSubnegEnd finds the end of a SB...SE subnegotiation block, returning the
// index just after SE and true, or (0, false) if the block is incomplete. It is
// a direct port of the Python _find_subneg_end static method.
func findSubnegEnd(buf []byte, start int) (int, bool) {
	j := start
	for j < len(buf)-1 {
		if buf[j] == iacByte && buf[j+1] == cmdSE {
			return j + 2, true
		}
		j++
	}
	return 0, false
}

// parseTelnetBuffer parses complete telnet sequences from buf. It returns the
// application payload bytes, the control events, and the number of bytes
// consumed. Trailing incomplete sequences are left unconsumed unless final is
// true, in which case they are flushed as literal data. This is a direct,
// branch-for-branch port of the Python _parse_telnet_buffer static method.
func parseTelnetBuffer(buf []byte, final bool) (payload []byte, events []telnetEvent, consumed int) {
	result := make([]byte, 0, len(buf))
	i := 0

	for i < len(buf) {
		if buf[i] != iacByte {
			result = append(result, buf[i])
			i++
			consumed = i
			continue
		}

		if i+1 >= len(buf) {
			if final {
				result = append(result, iacByte)
				i++
				consumed = i
			}
			break
		}

		cmd := buf[i+1]
		if cmd == cmdDO || cmd == cmdDONT || cmd == cmdWILL || cmd == cmdWONT {
			if i+2 >= len(buf) {
				if final {
					// Truncated negotiation: emit as literal data.
					result = append(result, buf[i:]...)
					i = len(buf)
					consumed = i
				}
				break
			}
			events = append(events, telnetEvent{kind: evNegotiate, cmd: cmd, opt: buf[i+2]})
			i += 3
			consumed = i
			continue
		}

		if cmd == cmdSB {
			end, ok := findSubnegEnd(buf, i+2)
			if !ok {
				if final {
					// Truncated subnegotiation: emit as literal data.
					result = append(result, buf[i:]...)
					i = len(buf)
					consumed = i
				}
				break
			}
			sub := make([]byte, end-2-(i+2))
			copy(sub, buf[i+2:end-2])
			events = append(events, telnetEvent{kind: evSubneg, payload: sub})
			i = end
			consumed = i
			continue
		}

		if cmd == iacByte {
			result = append(result, iacByte)
			i += 2
			consumed = i
			continue
		}

		i += 2
		consumed = i
	}

	return result, events, consumed
}
