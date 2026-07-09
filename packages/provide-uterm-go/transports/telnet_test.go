//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"sync"
	"testing"
	"time"
)

// startTCPServer starts a loopback TCP server that runs handler on the accepted
// connection. It returns host, port and registers cleanup.
func startTCPServer(t *testing.T, handler func(conn net.Conn)) (string, int) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })

	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		handler(conn)
	}()

	addr := ln.Addr().(*net.TCPAddr)
	return "127.0.0.1", addr.Port
}

// collectingHandler drains the connection into a buffer and signals via done.
func collectingHandler(initial []byte, buf *bytes.Buffer, mu *sync.Mutex, done chan<- struct{}) func(net.Conn) {
	return func(conn net.Conn) {
		defer close(done)
		if len(initial) > 0 {
			_, _ = conn.Write(initial)
		}
		tmp := make([]byte, 4096)
		for {
			n, err := conn.Read(tmp)
			if n > 0 {
				mu.Lock()
				buf.Write(tmp[:n])
				mu.Unlock()
			}
			if err != nil {
				return
			}
		}
	}
}

func TestTelnetConnectOffersBinaryAndSGA(t *testing.T) {
	var buf bytes.Buffer
	var mu sync.Mutex
	done := make(chan struct{})
	host, port := startTCPServer(t, collectingHandler(nil, &buf, &mu, done))

	tr := NewTelnetTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if !tr.IsConnected() {
		t.Fatal("expected connected")
	}
	if ip := tr.PeerIP(); ip != "127.0.0.1" {
		t.Errorf("PeerIP = %q, want 127.0.0.1", ip)
	}
	_ = tr.Disconnect(ctx)
	<-done

	mu.Lock()
	got := buf.Bytes()
	mu.Unlock()
	if !bytes.Contains(got, []byte{iacByte, cmdWILL, optBIN}) {
		t.Errorf("missing WILL BINARY in %v", got)
	}
	if !bytes.Contains(got, []byte{iacByte, cmdWILL, optSGA}) {
		t.Errorf("missing WILL SGA in %v", got)
	}
	if tr.IsConnected() {
		t.Error("expected disconnected")
	}
	if tr.PeerIP() != "" {
		t.Error("PeerIP should be empty after disconnect")
	}
}

func TestTelnetSendEscaping(t *testing.T) {
	var buf bytes.Buffer
	var mu sync.Mutex
	done := make(chan struct{})
	host, port := startTCPServer(t, collectingHandler(nil, &buf, &mu, done))

	tr := NewTelnetTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if err := tr.Send(ctx, []byte{0x7f, iacByte, 'h', 'i'}); err != nil {
		t.Fatalf("send: %v", err)
	}
	_ = tr.Disconnect(ctx)
	<-done

	mu.Lock()
	got := buf.Bytes()
	mu.Unlock()
	// DEL->BS and IAC doubled: 0x08, 0xFF 0xFF, h, i
	if !bytes.Contains(got, []byte{0x08, iacByte, iacByte, 'h', 'i'}) {
		t.Errorf("send escaping wrong: %v", got)
	}
	if bytes.Contains(got, []byte{0x7f}) {
		t.Error("DEL should have been remapped")
	}
}

func TestTelnetReceiveStripsIAC(t *testing.T) {
	// Server sends: data 'h' 'i', then IAC IAC (literal 0xFF), then 'x'.
	initial := []byte{'h', 'i', iacByte, iacByte, 'x'}
	host, port := startTCPServer(t, func(conn net.Conn) {
		_, _ = conn.Write(initial)
		time.Sleep(200 * time.Millisecond)
		_ = conn.Close()
	})

	tr := NewTelnetTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	got, err := tr.Receive(ctx, 4096, time.Second)
	if err != nil {
		t.Fatalf("receive: %v", err)
	}
	if !bytes.Equal(got, []byte{'h', 'i', iacByte, 'x'}) {
		t.Errorf("received %v, want h i 0xFF x", got)
	}
	_ = tr.Disconnect(ctx)
}

func TestTelnetReceiveTimeout(t *testing.T) {
	host, port := startTCPServer(t, func(conn net.Conn) {
		time.Sleep(500 * time.Millisecond)
		_ = conn.Close()
	})
	tr := NewTelnetTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	got, err := tr.Receive(ctx, 4096, 50*time.Millisecond)
	if err != nil {
		t.Fatalf("timeout should be nil error, got %v", err)
	}
	if len(got) != 0 {
		t.Errorf("timeout should return empty, got %v", got)
	}
	_ = tr.Disconnect(ctx)
}

func TestTelnetRemoteCloseWithLeftover(t *testing.T) {
	// Send a lone trailing IAC then close: parser (final) emits it as literal.
	host, port := startTCPServer(t, func(conn net.Conn) {
		_, _ = conn.Write([]byte{'b', 'y', 'e', iacByte})
		_ = conn.Close()
	})
	tr := NewTelnetTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	// First receive: server may deliver data before EOF; loop until we see it.
	deadline := time.Now().Add(2 * time.Second)
	var got []byte
	for time.Now().Before(deadline) {
		out, err := tr.Receive(ctx, 4096, 200*time.Millisecond)
		if len(out) > 0 {
			got = out
			break
		}
		if errors.Is(err, ErrConnectionClosed) {
			got = out
			break
		}
	}
	if !bytes.Contains(got, []byte{'b', 'y', 'e'}) {
		t.Errorf("leftover payload missing: %v", got)
	}
}

func TestTelnetRemoteCloseNoLeftover(t *testing.T) {
	host, port := startTCPServer(t, func(conn net.Conn) {
		_ = conn.Close()
	})
	tr := NewTelnetTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	var lastErr error
	for i := 0; i < 20; i++ {
		_, err := tr.Receive(ctx, 4096, 200*time.Millisecond)
		if err != nil {
			lastErr = err
			break
		}
	}
	if !errors.Is(lastErr, ErrConnectionClosed) {
		t.Errorf("want ErrConnectionClosed, got %v", lastErr)
	}
	if tr.IsConnected() {
		t.Error("should be disconnected after remote close")
	}
}

// TestTelnetNegotiationExchange drives every negotiation branch: the server
// sends a batch of DO/WILL/WONT/DONT and a TTYPE SEND subnegotiation; the
// client must answer synchronously inside Receive.
func TestTelnetNegotiationExchange(t *testing.T) {
	server := []byte{
		iacByte, cmdDO, optNAWS, // -> WILL NAWS + NAWS subneg
		iacByte, cmdDO, optTTYPE, // -> WILL TTYPE + TTYPE IS
		iacByte, cmdDO, optBIN, // -> WILL BINARY (already sent on connect: dedup, no repeat)
		iacByte, cmdDO, 99, // unknown -> WONT 99
		iacByte, cmdWILL, optECHO, // -> DO ECHO
		iacByte, cmdWILL, 77, // unknown -> DONT 77
		iacByte, cmdWONT, optECHO, // -> DONT ECHO (already: dedup)
		iacByte, cmdDONT, optSGA, // -> WONT SGA (already sent WILL SGA; WONT is new)
		iacByte, cmdSB, optTTYPE, 1, iacByte, cmdSE, // TTYPE SEND -> TTYPE IS
		'o', 'k',
	}
	var buf bytes.Buffer
	var mu sync.Mutex
	host, port := startTCPServer(t, func(conn net.Conn) {
		_, _ = conn.Write(server)
		tmp := make([]byte, 4096)
		for {
			n, err := conn.Read(tmp)
			if n > 0 {
				mu.Lock()
				buf.Write(tmp[:n])
				mu.Unlock()
			}
			if err != nil {
				return
			}
		}
	})

	tr := NewTelnetTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{Cols: 80, Rows: 25, Term: "VT100"}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	got, err := tr.Receive(ctx, 4096, time.Second)
	if err != nil {
		t.Fatalf("receive: %v", err)
	}
	if !bytes.Equal(got, []byte("ok")) {
		t.Errorf("payload = %q, want ok", got)
	}
	// Give the client's writes time to reach the server.
	time.Sleep(100 * time.Millisecond)
	_ = tr.Disconnect(ctx)

	mu.Lock()
	resp := buf.Bytes()
	mu.Unlock()
	wants := map[string][]byte{
		"WILL NAWS":   {iacByte, cmdWILL, optNAWS},
		"NAWS subneg": {iacByte, cmdSB, optNAWS, 0, 80, 0, 25, iacByte, cmdSE},
		"WILL TTYPE":  {iacByte, cmdWILL, optTTYPE},
		"TTYPE IS":    append([]byte{iacByte, cmdSB, optTTYPE, ttypeIS}, []byte("VT100")...),
		"WONT 99":     {iacByte, cmdWONT, 99},
		"DO ECHO":     {iacByte, cmdDO, optECHO},
		"DONT 77":     {iacByte, cmdDONT, 77},
		"WONT SGA":    {iacByte, cmdWONT, optSGA},
	}
	for name, want := range wants {
		if !bytes.Contains(resp, want) {
			t.Errorf("missing response %s (%v) in %v", name, want, resp)
		}
	}
}

func TestTelnetSetSize(t *testing.T) {
	var buf bytes.Buffer
	var mu sync.Mutex
	done := make(chan struct{})
	host, port := startTCPServer(t, collectingHandler(nil, &buf, &mu, done))

	tr := NewTelnetTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if err := tr.SetSize(ctx, 512, 256); err != nil {
		t.Fatalf("setsize: %v", err)
	}
	time.Sleep(50 * time.Millisecond)
	_ = tr.Disconnect(ctx)
	<-done

	mu.Lock()
	got := buf.Bytes()
	mu.Unlock()
	// 512=0x0200 -> 2,0 ; 256=0x0100 -> 1,0
	want := []byte{iacByte, cmdSB, optNAWS, 2, 0, 1, 0, iacByte, cmdSE}
	if !bytes.Contains(got, want) {
		t.Errorf("NAWS packet missing: %v", got)
	}
}

func TestTelnetRxBufferCap(t *testing.T) {
	// Send IAC SB then a flood of non-SE bytes: the parser never completes the
	// subnegotiation, so the rx buffer grows past the cap.
	host, port := startTCPServer(t, func(conn net.Conn) {
		_, _ = conn.Write([]byte{iacByte, cmdSB})
		flood := bytes.Repeat([]byte{'A'}, maxRxBufBytes+8192)
		_, _ = conn.Write(flood)
		time.Sleep(time.Second)
		_ = conn.Close()
	})
	tr := NewTelnetTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	var capErr error
	for i := 0; i < 500; i++ {
		_, err := tr.Receive(ctx, 65536, 200*time.Millisecond)
		if err != nil {
			capErr = err
			break
		}
	}
	if capErr == nil || !bytes.Contains([]byte(capErr.Error()), []byte("receive buffer exceeded")) {
		t.Errorf("want buffer-exceeded error, got %v", capErr)
	}
}

func TestTelnetNotConnectedErrors(t *testing.T) {
	tr := NewTelnetTransport()
	ctx := context.Background()
	if err := tr.Send(ctx, []byte("x")); !errors.Is(err, ErrNotConnected) {
		t.Errorf("send: want ErrNotConnected, got %v", err)
	}
	if _, err := tr.Receive(ctx, 10, time.Millisecond); !errors.Is(err, ErrNotConnected) {
		t.Errorf("receive: want ErrNotConnected, got %v", err)
	}
	if err := tr.SetSize(ctx, 80, 25); !errors.Is(err, ErrNotConnected) {
		t.Errorf("setsize: want ErrNotConnected, got %v", err)
	}
	// Disconnect on a fresh transport is a no-op.
	if err := tr.Disconnect(ctx); err != nil {
		t.Errorf("disconnect noop: %v", err)
	}
}

func TestTelnetConnectFailure(t *testing.T) {
	tr := NewTelnetTransport()
	// Port 1 on loopback should refuse quickly.
	err := tr.Connect(context.Background(), "127.0.0.1", 1, ConnectOptions{Timeout: 200 * time.Millisecond})
	if err == nil {
		t.Fatal("expected connect failure")
	}
}

func TestTelnetConnectReplacesExisting(t *testing.T) {
	host, port := startTCPServer(t, func(conn net.Conn) {
		_, _ = io.Copy(io.Discard, conn)
	})
	host2, port2 := startTCPServer(t, func(conn net.Conn) {
		_, _ = io.Copy(io.Discard, conn)
	})
	tr := NewTelnetTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{}); err != nil {
		t.Fatalf("connect1: %v", err)
	}
	// Second connect should disconnect the first transparently.
	if err := tr.Connect(ctx, host2, port2, ConnectOptions{}); err != nil {
		t.Fatalf("connect2: %v", err)
	}
	if !tr.IsConnected() {
		t.Error("expected connected after reconnect")
	}
	_ = tr.Disconnect(ctx)
}
