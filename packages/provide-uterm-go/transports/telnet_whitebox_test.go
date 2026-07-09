//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"bytes"
	"context"
	"net"
	"testing"
	"time"
)

// TestTelnetWriteHelpersNilConnGuards exercises the "no connection" guard in
// every write helper directly (these guards are otherwise defensive).
func TestTelnetWriteHelpersNilConnGuards(t *testing.T) {
	tr := NewTelnetTransport() // conn is nil
	tr.mu.Lock()
	defer tr.mu.Unlock()
	tr.writeLocked([]byte{1})                          // nil-conn guard
	tr.sendNAWSLocked(80, 25)                          // nil-conn guard
	tr.sendSubnegotiationLocked([]byte{1, 2})          // nil-conn guard
	tr.sendTTYPELocked("ANSI")                         // -> sendSubnegotiation nil-conn guard
	tr.negotiateLocked(cmdDO, optBIN)                  // nil-conn guard (early return)
	tr.handleSubnegotiationLocked([]byte{})            // empty guard
	tr.handleSubnegotiationLocked([]byte{optTTYPE, 1}) // nil-conn guard inside
}

// TestTelnetHandleSubnegotiationLive covers the live TTYPE-SEND response and the
// non-matching-first-byte no-op via an in-memory pipe.
func TestTelnetHandleSubnegotiationLive(t *testing.T) {
	c1, c2 := net.Pipe()
	defer func() { _ = c1.Close() }()
	defer func() { _ = c2.Close() }()

	tr := NewTelnetTransport()
	tr.conn = c1
	tr.term = "ANSI"

	got := make(chan []byte, 1)
	go func() {
		buf := make([]byte, 64)
		_ = c2.SetReadDeadline(time.Now().Add(time.Second))
		n, _ := c2.Read(buf)
		got <- buf[:n]
	}()

	tr.mu.Lock()
	tr.handleSubnegotiationLocked([]byte{optTTYPE, 1}) // TTYPE SEND -> writes TTYPE IS
	tr.mu.Unlock()

	select {
	case out := <-got:
		want := append([]byte{iacByte, cmdSB, optTTYPE, ttypeIS}, []byte("ANSI")...)
		want = append(want, iacByte, cmdSE)
		if !bytes.Equal(out, want) {
			t.Errorf("TTYPE IS = %v, want %v", out, want)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no TTYPE IS response written")
	}

	// Non-matching first byte writes nothing (drain-free no-op).
	tr.mu.Lock()
	tr.handleSubnegotiationLocked([]byte{0x05, 0x06})
	tr.mu.Unlock()
}

// TestTelnetHandleRemoteCloseLeftover covers the residual-buffer flush path
// (consumed>0 and payload return) that the network tests race past.
func TestTelnetHandleRemoteCloseLeftover(t *testing.T) {
	c1, c2 := net.Pipe()
	defer func() { _ = c1.Close() }()
	defer func() { _ = c2.Close() }()
	tr := NewTelnetTransport()
	tr.conn = c1
	// Trailing lone IAC: final parse emits it as literal, consumed>0.
	tr.rxBuf = []byte{'h', 'i', iacByte}

	payload, err := tr.handleRemoteClose(context.Background())
	if err != nil {
		t.Fatalf("expected leftover payload, got err %v", err)
	}
	if !bytes.Equal(payload, []byte{'h', 'i', iacByte}) {
		t.Errorf("payload = %v", payload)
	}
	if tr.IsConnected() {
		t.Error("handleRemoteClose should disconnect")
	}
}

// TestTelnetReceiveSetDeadlineError covers the SetReadDeadline error branch by
// using a closed pipe endpoint.
func TestTelnetReceiveSetDeadlineError(t *testing.T) {
	c1, c2 := net.Pipe()
	_ = c1.Close()
	_ = c2.Close()
	tr := NewTelnetTransport()
	tr.conn = c1
	if _, err := tr.Receive(context.Background(), 16, 10*time.Millisecond); err == nil {
		t.Fatal("expected error from Receive on closed conn")
	}
}

// TestTelnetSendWriteError covers the Send write-error path: the peer end of a
// pipe is closed so conn.Write fails, forcing Disconnect + error.
func TestTelnetSendWriteError(t *testing.T) {
	c1, c2 := net.Pipe()
	_ = c2.Close() // reads impossible -> writes on c1 fail

	tr := NewTelnetTransport()
	tr.conn = c1
	tr.negWill = map[byte]bool{}
	tr.negWont = map[byte]bool{}
	tr.negDo = map[byte]bool{}
	tr.negDont = map[byte]bool{}

	err := tr.Send(context.Background(), []byte("data"))
	if err == nil {
		t.Fatal("expected send write error")
	}
	if tr.IsConnected() {
		t.Error("Send should have disconnected on write error")
	}
	_ = c1.Close()
}
