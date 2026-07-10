//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package gateway ports the reverse-direction gateway classes of provide-uterm
// (provide.uterm.gateway) to Go: a raw-TCP (telnet) listener and an SSH server
// that each accept inbound clients and proxy every byte to an upstream
// WebSocket terminal server through the inline DLE/STX control channel.
//
// It is the mirror image of the browser-facing proxy: instead of a browser
// WebSocket dialing out to telnet/SSH, traditional telnet/SSH clients dial in
// and are bridged out to a WebSocket. See TelnetWsGateway and SshWsGateway.
package gateway

import "strings"

// Telnet byte vocabulary (RFC 854 + option codes). Mirrors _iac_negotiate.py.
const (
	iacIAC  = 255
	iacDONT = 254
	iacDO   = 253
	iacWONT = 252
	iacWILL = 251
	iacSB   = 250
	iacSE   = 240

	iacBREAK = 243
	iacIP    = 244
	iacEOF   = 236

	optTTYPE      = 24 // RFC 1091
	optNewEnviron = 39 // RFC 1572

	subIS      = 0
	subSEND    = 1
	envVar     = 0
	envValue   = 1
	envEsc     = 2
	envUserVar = 3

	// maxSBBytes bounds a single IAC subnegotiation so a hostile client that
	// opens IAC SB and never sends IAC SE cannot grow the buffer without limit.
	maxSBBytes = 4096
)

// DeriveColormode picks the best ?colormode= value from a TERM + env map.
//
// Precedence (first match wins; "" when nothing applies):
//  1. COLORTERM == truecolor / 24bit → passthrough
//  2. TERM ending in -direct / -truecolor → passthrough
//  3. TERM ending in -256color → 256
//  4. Legacy TERM (xterm / vt100 / …) → 16
//
// Case-insensitive; a missing term falls back to env["TERM"].
func DeriveColormode(term string, env map[string]string) string {
	colorterm := strings.ToLower(strings.TrimSpace(env["COLORTERM"]))
	if colorterm == "truecolor" || colorterm == "24bit" {
		return "passthrough"
	}
	t := term
	if t == "" {
		t = env["TERM"]
	}
	t = strings.ToLower(strings.TrimSpace(t))
	if strings.HasSuffix(t, "-direct") || strings.HasSuffix(t, "-truecolor") || t == "xterm-direct" {
		return "passthrough"
	}
	if strings.HasSuffix(t, "-256color") || t == "xterm-256color" {
		return "256"
	}
	switch t {
	case "xterm", "vt100", "vt102", "vt220", "ansi", "linux", "dumb":
		return "16"
	}
	return ""
}

// stripIAC removes IAC telnet negotiation sequences from inbound client data,
// translating IP/BREAK to Ctrl-C and EOF to Ctrl-D. Stateless fallback used
// when IAC negotiation is disabled (mirrors _strip_iac).
func stripIAC(data []byte) []byte {
	out := make([]byte, 0, len(data))
	i, n := 0, len(data)
	for i < n {
		b := data[i]
		if b != iacIAC {
			out = append(out, b)
			i++
			continue
		}
		if i+1 >= n {
			break
		}
		cmd := data[i+1]
		switch cmd {
		case iacIAC:
			out = append(out, iacIAC)
			i += 2
		case iacSB:
			i = skipSubneg(data, i+2, n)
		case iacIP, iacBREAK:
			out = append(out, 0x03)
			i += 2
		case iacEOF:
			out = append(out, 0x04)
			i += 2
		case iacWILL, iacWONT, iacDO, iacDONT:
			if i+2 >= n {
				return out
			}
			i += 3
		default:
			i += 2
		}
	}
	return out
}

// skipSubneg scans forward from i (first byte after IAC SB) to just past the
// closing IAC SE pair, or n if the sequence is truncated.
func skipSubneg(data []byte, i, n int) int {
	for i < n {
		if data[i] == iacIAC && i+1 < n && data[i+1] == iacSE {
			return i + 2
		}
		i++
	}
	return n
}

// IacNegotiator is a stateful IAC negotiator: it reads client bytes, emits
// replies, and collects TERM/env hints. Single-use per TCP connection. Direct
// port of the Python IacNegotiator.
type IacNegotiator struct {
	Term string
	Env  map[string]string

	sbOption   int // -1 when not inside a subnegotiation
	sbBuf      []byte
	sbOverflow bool
	pending    []byte

	ttypeRequested      bool
	newEnvironRequested bool
	ttypeReceived       bool
	newEnvironReceived  bool
}

// NewIacNegotiator returns a fresh negotiator.
func NewIacNegotiator() *IacNegotiator {
	return &IacNegotiator{Env: map[string]string{}, sbOption: -1}
}

// StartBytes returns the initial IAC bytes to send on session start
// (IAC DO TTYPE, IAC DO NEW-ENVIRON).
func (g *IacNegotiator) StartBytes() []byte {
	g.ttypeRequested = true
	g.newEnvironRequested = true
	return []byte{iacIAC, iacDO, optTTYPE, iacIAC, iacDO, optNewEnviron}
}

// Feed consumes data from the client and returns (reply, cleaned): reply is
// bytes to echo back to the client, cleaned is the application data with IAC
// noise removed.
func (g *IacNegotiator) Feed(data []byte) (reply, cleaned []byte) {
	if len(g.pending) > 0 {
		data = append(append([]byte(nil), g.pending...), data...)
		g.pending = nil
	}
	var rep, clean []byte
	i, n := 0, len(data)
	for i < n {
		if g.sbOption >= 0 {
			if data[i] == iacIAC && i+1 < n && data[i+1] == iacSE {
				g.finishSB()
				i += 2
				continue
			}
			if data[i] == iacIAC && i+1 < n && data[i+1] == iacIAC {
				g.appendSB(iacIAC)
				i += 2
				continue
			}
			g.appendSB(data[i])
			i++
			continue
		}
		b := data[i]
		if b != iacIAC {
			clean = append(clean, b)
			i++
			continue
		}
		if i+1 >= n {
			g.pending = append([]byte(nil), data[i:]...)
			break
		}
		cmd := data[i+1]
		switch cmd {
		case iacIAC:
			clean = append(clean, iacIAC)
			i += 2
		case iacSB:
			if i+2 >= n {
				g.pending = append([]byte(nil), data[i:]...)
				return rep, clean
			}
			g.sbOption = int(data[i+2])
			g.sbBuf = nil
			g.sbOverflow = false
			i += 3
		case iacWILL, iacWONT, iacDO, iacDONT:
			if i+2 >= n {
				g.pending = append([]byte(nil), data[i:]...)
				return rep, clean
			}
			rep = append(rep, g.handleOption(cmd, data[i+2])...)
			i += 3
		default:
			i += 2
		}
	}
	return rep, clean
}

// Done reports whether every requested option has been answered.
func (g *IacNegotiator) Done() bool {
	ttypeOK := !g.ttypeRequested || g.ttypeReceived
	envOK := !g.newEnvironRequested || g.newEnvironReceived
	return ttypeOK && envOK
}

// DerivedColormode maps the captured hints to a ?colormode= value.
func (g *IacNegotiator) DerivedColormode() string { return DeriveColormode(g.Term, g.Env) }

func (g *IacNegotiator) handleOption(verb, option byte) []byte {
	if verb == iacWILL && option == optTTYPE {
		return []byte{iacIAC, iacSB, optTTYPE, subSEND, iacIAC, iacSE}
	}
	if verb == iacWILL && option == optNewEnviron {
		return []byte{iacIAC, iacSB, optNewEnviron, subSEND, iacIAC, iacSE}
	}
	return nil
}

func (g *IacNegotiator) appendSB(b byte) {
	if len(g.sbBuf) >= maxSBBytes {
		g.sbOverflow = true
		return
	}
	g.sbBuf = append(g.sbBuf, b)
}

func (g *IacNegotiator) finishSB() {
	option := g.sbOption
	payload := g.sbBuf
	overflowed := g.sbOverflow
	g.sbOption = -1
	g.sbBuf = nil
	g.sbOverflow = false
	if overflowed {
		return
	}
	switch option {
	case optTTYPE:
		g.Term = parseTTypeIS(payload)
		g.ttypeReceived = true
	case optNewEnviron:
		g.Env = parseNewEnvironIS(payload)
		g.newEnvironReceived = true
	}
}

// parseTTypeIS extracts the lowercased terminal name from a TTYPE IS payload.
func parseTTypeIS(payload []byte) string {
	if len(payload) == 0 || payload[0] != subIS {
		return ""
	}
	return strings.ToLower(strings.TrimSpace(string(payload[1:])))
}

// parseNewEnvironIS parses an RFC 1572 NEW-ENVIRON IS payload into a map.
func parseNewEnvironIS(payload []byte) map[string]string {
	out := map[string]string{}
	if len(payload) == 0 || payload[0] != subIS {
		return out
	}
	i, n := 1, len(payload)
	for i < n {
		marker := payload[i]
		if marker != envVar && marker != envUserVar {
			return out
		}
		i++
		var name []byte
		for i < n && payload[i] != envValue && payload[i] != envVar && payload[i] != envUserVar {
			if payload[i] == envEsc && i+1 < n {
				name = append(name, payload[i+1])
				i += 2
				continue
			}
			name = append(name, payload[i])
			i++
		}
		var value []byte
		if i < n && payload[i] == envValue {
			i++
			for i < n && payload[i] != envVar && payload[i] != envUserVar {
				if payload[i] == envEsc && i+1 < n {
					value = append(value, payload[i+1])
					i += 2
					continue
				}
				value = append(value, payload[i])
				i++
			}
		}
		nm := strings.TrimSpace(string(name))
		if nm != "" {
			out[nm] = string(value)
		}
	}
	return out
}
