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
