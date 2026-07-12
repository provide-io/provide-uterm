//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

// Exported telnet protocol constants, mirroring the public surface of the
// Python telnet module (transports/telnet.py). The full TelnetTransport client
// supersedes the thin TelnetClient wrapper from telnet_client.py, so only the
// constants and IAC sequence builders are ported here as the public helper set.
const (
	// TelnetIAC is Interpret-As-Command (0xFF).
	TelnetIAC = iacByte
	// TelnetWILL signals willingness to perform an option.
	TelnetWILL = cmdWILL
	// TelnetWONT signals refusal to perform an option.
	TelnetWONT = cmdWONT
	// TelnetDO requests the peer perform an option.
	TelnetDO = cmdDO
	// TelnetDONT requests the peer not perform an option.
	TelnetDONT = cmdDONT
	// TelnetSB begins a subnegotiation.
	TelnetSB = cmdSB
	// TelnetSE ends a subnegotiation.
	TelnetSE = cmdSE
	// TelnetECHO is the ECHO option.
	TelnetECHO = optECHO
	// TelnetSGA is the Suppress-Go-Ahead option.
	TelnetSGA = optSGA
	// TelnetNAWS is the Negotiate-About-Window-Size option.
	TelnetNAWS = optNAWS
	// TelnetTTYPE is the Terminal-Type option.
	TelnetTTYPE = optTTYPE
	// TelnetBINARY is the BINARY-transmission option.
	TelnetBINARY = optBIN
)

// BuildWill returns an IAC WILL <option> sequence (port of TelnetClient.will).
func BuildWill(option byte) []byte { return []byte{iacByte, cmdWILL, option} }

// BuildWont returns an IAC WONT <option> sequence (port of TelnetClient.wont).
func BuildWont(option byte) []byte { return []byte{iacByte, cmdWONT, option} }

// BuildDo returns an IAC DO <option> sequence (port of TelnetClient.do).
func BuildDo(option byte) []byte { return []byte{iacByte, cmdDO, option} }

// BuildDont returns an IAC DONT <option> sequence (port of TelnetClient.dont).
func BuildDont(option byte) []byte { return []byte{iacByte, cmdDONT, option} }

// EscapeIAC doubles 0xFF bytes for binary-safe telnet send.
func EscapeIAC(data []byte) []byte {
	out := make([]byte, 0, len(data)+4)
	for _, b := range data {
		out = append(out, b)
		if b == iacByte {
			out = append(out, iacByte)
		}
	}
	return out
}

// TelnetEvent is a public negotiation/subnegotiation event from ParseTelnetBuffer.
type TelnetEvent struct {
	IsSubneg bool
	Cmd      byte
	Opt      byte
	Payload  []byte // for subneg: bytes between SB and IAC SE (includes option)
}

// ParseTelnetBuffer is the public form of the RFC 854 parser used by embed sessions.
func ParseTelnetBuffer(buf []byte, final bool) (payload []byte, events []TelnetEvent, consumed int) {
	p, evs, c := parseTelnetBuffer(buf, final)
	out := make([]TelnetEvent, 0, len(evs))
	for _, e := range evs {
		switch e.kind {
		case evNegotiate:
			out = append(out, TelnetEvent{IsSubneg: false, Cmd: e.cmd, Opt: e.opt})
		case evSubneg:
			out = append(out, TelnetEvent{IsSubneg: true, Payload: append([]byte(nil), e.payload...)})
		}
	}
	return p, out, c
}
