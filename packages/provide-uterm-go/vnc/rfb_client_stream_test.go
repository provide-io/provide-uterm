// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package vnc

import (
	"bytes"
	"errors"
	"net"
	"strings"
	"sync"
	"testing"
	"time"
)

// streamConn is a synchronous net.Conn stand-in: reads drain a fixed script
// and then report EOF, writes are recorded (or refused). It lets readLoop run
// on the test goroutine, so each exit is asserted directly instead of being
// raced against a background reader.
type streamConn struct {
	net.Conn
	r        *bytes.Reader
	reads    int
	afterRd  func(n int)
	writeErr error
	mu       sync.Mutex
	written  [][]byte
}

func (s *streamConn) Read(b []byte) (int, error) {
	n, err := s.r.Read(b)
	s.reads++
	if s.afterRd != nil {
		s.afterRd(s.reads)
	}
	return n, err
}

func (s *streamConn) Write(b []byte) (int, error) {
	if s.writeErr != nil {
		return 0, s.writeErr
	}
	s.mu.Lock()
	s.written = append(s.written, append([]byte(nil), b...))
	s.mu.Unlock()
	return len(b), nil
}

func newStreamClient(conn net.Conn) *RFBClient {
	return &RFBClient{
		conn:    conn,
		tracker: NewFramebufferTracker(4, 2),
		width:   4,
		height:  2,
		done:    make(chan struct{}),
	}
}

func runReadLoop(t *testing.T, c *RFBClient) {
	t.Helper()
	c.readLoop()
	select {
	case <-c.done:
	default:
		t.Fatal("readLoop returned without closing done")
	}
}

// Every truncation point inside an update ends the loop without asking the
// server for another update: a desynchronised stream must not be extended.
func TestReadLoopStopsOnTruncatedUpdates(t *testing.T) {
	full := rawUpdateBytes(0, 0, 1, 1, []byte{1, 2, 3, 4})
	for _, tc := range []struct {
		name string
		cut  int
	}{
		{"inside the update header", 2},
		{"inside a rectangle header", 4 + 6},
		{"inside the raw pixels", 4 + 12 + 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			conn := &streamConn{r: bytes.NewReader(full[:tc.cut])}
			runReadLoop(t, newStreamClient(conn))
			if len(conn.written) != 0 {
				t.Fatalf("update requested after a truncated update: %v", conn.written)
			}
		})
	}
}

// A Close that lands while a batch of rectangles is being applied suppresses
// the follow-up update request.
func TestReadLoopDoesNotRequestAfterCloseMidUpdate(t *testing.T) {
	update := make([]byte, 4) // type 0, padding, zero rectangles
	conn := &streamConn{r: bytes.NewReader(update)}
	c := newStreamClient(conn)
	conn.afterRd = func(n int) {
		if n == 2 { // the 3-byte remainder of the update header
			c.mu.Lock()
			c.closed = true
			c.mu.Unlock()
		}
	}
	runReadLoop(t, c)
	if len(conn.written) != 0 {
		t.Fatalf("update requested after Close: %v", conn.written)
	}
}

// A refused update-request write ends the loop instead of reading on.
func TestReadLoopStopsWhenTheUpdateRequestFails(t *testing.T) {
	update := make([]byte, 4)
	extra := rawUpdateBytes(0, 0, 1, 1, []byte{1, 2, 3, 4})
	conn := &streamConn{r: bytes.NewReader(append(update, extra...)), writeErr: errors.New("write refused")}
	runReadLoop(t, newStreamClient(conn))
	if conn.r.Len() != len(extra) {
		t.Fatalf("loop kept reading after the write failed: %d bytes left, want %d", conn.r.Len(), len(extra))
	}
}

// RFB 3.3 has the server dictate the security type; a peer that hangs up
// before sending it fails the handshake with that step named.
func TestSecurity33TruncatedTypeIsReported(t *testing.T) {
	conn := &streamConn{r: bytes.NewReader([]byte{0, 0})}
	err := negotiateSecurity(conn, "RFB 003.003\n")
	if err == nil || !strings.HasPrefix(err.Error(), "rfb security type: ") {
		t.Fatalf("err = %v", err)
	}
}

// A ServerInit announcing a desktop name it then never sends is an error.
func TestServerInitTruncatedDesktopNameIsReported(t *testing.T) {
	init := serverInitBytes(64, 64, "desktop")
	conn := &streamConn{r: bytes.NewReader(init[:len(init)-3])}
	_, _, err := readServerInit(conn)
	if err == nil || !strings.HasPrefix(err.Error(), "rfb desktop name: ") {
		t.Fatalf("err = %v", err)
	}
}

// With no dialer injected, DialRFB dials a real TCP socket: a listener that
// accepts and hangs up proves the connection was made (the failure is the
// handshake's, not the dial's).
func TestDialRFBDefaultDialerUsesTCP(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = ln.Close() }()
	accepted := make(chan struct{})
	go func() {
		conn, aErr := ln.Accept()
		if aErr == nil {
			_ = conn.Close()
		}
		close(accepted)
	}()
	addr := ln.Addr().(*net.TCPAddr)
	_, err = DialRFB("127.0.0.1", addr.Port, 2*time.Second, nil)
	<-accepted
	if err == nil || !strings.HasPrefix(err.Error(), "rfb protocol version: ") {
		t.Fatalf("err = %v, want a handshake failure after a real dial", err)
	}
}
