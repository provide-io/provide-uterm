//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"bytes"
	"context"
	"net"
	"testing"
	"time"
)

// TestHandleConnOversizedAndParseErrors drives handleConn over a net.Pipe: an
// oversized line (ErrBufferFull -> discardToNewline), a non-JSON line
// (parseEvent !ok), then a valid event that must still be delivered.
func TestHandleConnOversizedAndParseErrors(t *testing.T) {
	l := &PamNotifyListener{conns: map[net.Conn]struct{}{}}
	got := make(chan PamEvent, 4)
	l.handler = func(_ context.Context, ev PamEvent) { got <- ev }

	c1, c2 := net.Pipe()
	go l.handleConn(context.Background(), c2)

	go func() {
		// Oversized line with no newline until past the buffer -> ErrBufferFull.
		_, _ = c1.Write(bytes.Repeat([]byte("A"), notifyMaxLine*2))
		_, _ = c1.Write([]byte("\n"))
		// A line that fails JSON parsing -> parseEvent returns ok=false.
		_, _ = c1.Write([]byte("garbage-not-json\n"))
		// A valid event that must be delivered.
		_, _ = c1.Write([]byte(`{"event":"open","username":"z","pid":7}` + "\n"))
	}()

	select {
	case ev := <-got:
		if ev.Username != "z" {
			t.Fatalf("unexpected event: %+v", ev)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("valid event after oversized + bad line was not delivered")
	}
	_ = c1.Close()
	_ = c2.Close()
}
