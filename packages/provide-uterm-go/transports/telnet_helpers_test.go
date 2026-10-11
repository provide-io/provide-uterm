//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"bytes"
	"testing"
)

func TestEscapeIACPublic(t *testing.T) {
	got := EscapeIAC([]byte{0x01, 0xff, 0x02})
	want := []byte{0x01, 0xff, 0xff, 0x02}
	if !bytes.Equal(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
	if EscapeIAC(nil) != nil && len(EscapeIAC(nil)) != 0 {
		t.Fatalf("nil/empty")
	}
}

func TestParseTelnetBufferPublic(t *testing.T) {
	payload, events, n := ParseTelnetBuffer([]byte("Hi\xff\xff!"), true)
	if string(payload) != "Hi\xff!" || n != 5 {
		t.Fatalf("payload=%q n=%d events=%v", payload, n, events)
	}
	// DO BINARY should surface a negotiation event
	p, ev, _ := ParseTelnetBuffer([]byte{255, 253, 0}, true)
	if len(p) != 0 || len(ev) == 0 {
		t.Fatalf("expected event, payload=%v events=%v", p, ev)
	}
}

// TestParseTelnetBufferPublicSubnegotiation proves an SB..IAC SE block surfaces
// as a subnegotiation event carrying the option byte and body, with no payload
// bytes leaking into the data stream, and that the payload is a copy.
func TestParseTelnetBufferPublicSubnegotiation(t *testing.T) {
	buf := []byte{'a', 255, 250, 24, 1, 255, 240, 'b'} // IAC SB TTYPE SEND IAC SE
	payload, events, n := ParseTelnetBuffer(buf, true)
	if string(payload) != "ab" || n != len(buf) {
		t.Fatalf("payload=%q n=%d", payload, n)
	}
	if len(events) != 1 || !events[0].IsSubneg || string(events[0].Payload) != "\x18\x01" {
		t.Fatalf("events = %+v", events)
	}
	buf[3] = 0
	if events[0].Payload[0] != 24 {
		t.Fatal("subnegotiation payload must not alias the input buffer")
	}
}
