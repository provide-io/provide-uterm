//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

// startTCPEcho starts a loopback TCP server that echoes the initial banner then
// echoes any received bytes back. Returns host and port.
func startTCPEcho(t *testing.T, banner []byte) (string, int) {
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
		defer func() { _ = conn.Close() }()
		if len(banner) > 0 {
			_, _ = conn.Write(banner)
		}
		buf := make([]byte, 4096)
		for {
			n, err := conn.Read(buf)
			if n > 0 {
				_, _ = conn.Write(buf[:n])
			}
			if err != nil {
				return
			}
		}
	}()
	addr := ln.Addr().(*net.TCPAddr)
	return "127.0.0.1", addr.Port
}

// TestTelnetConnectorLoopback dials a loopback TCP server and reads its banner
// into the emulated screen.
func TestTelnetConnectorLoopback(t *testing.T) {
	host, port := startTCPEcho(t, []byte("WELCOME telnet\r\n"))
	c, err := newTelnet("tn", "Telnet", map[string]any{"host": host, "port": port})
	if err != nil {
		t.Fatalf("newTelnet: %v", err)
	}
	ctx := context.Background()
	if err := c.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer func() { _ = c.Stop(ctx) }()

	if err := waitForScreen(ctx, c, "WELCOME telnet", 3*time.Second); err != nil {
		t.Fatalf("telnet banner: %v\nscreen:\n%s", err, c.Snapshot().Screen)
	}
	if events := c.Events(); len(events) == 0 {
		t.Fatal("expected buffered term events from telnet upstream")
	}
}

// TestTelnetConnectorDefaults exercises the default host/port branch.
func TestTelnetConnectorDefaults(t *testing.T) {
	c, err := newTelnet("tn", "Telnet", nil)
	if err != nil {
		t.Fatalf("newTelnet: %v", err)
	}
	if !strings.HasPrefix(c.upstream, "telnet://") {
		t.Fatalf("unexpected upstream: %s", c.upstream)
	}
}

// TestWebSocketConnectorLoopback dials an httptest echo server, sends input and
// reads the echo back into the emulated screen.
func TestWebSocketConnectorLoopback(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		defer func() { _ = conn.CloseNow() }()
		ctx := r.Context()
		for {
			typ, data, err := conn.Read(ctx)
			if err != nil {
				return
			}
			if err := conn.Write(ctx, typ, data); err != nil {
				return
			}
		}
	}))
	t.Cleanup(srv.Close)

	url := "ws" + strings.TrimPrefix(srv.URL, "http")
	c, err := newWebSocket("ws", "WS", map[string]any{"url": url})
	if err != nil {
		t.Fatalf("newWebSocket: %v", err)
	}
	ctx := context.Background()
	if err := c.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer func() { _ = c.Stop(ctx) }()

	if err := c.HandleInput(ctx, "ws-hello"); err != nil {
		t.Fatalf("HandleInput: %v", err)
	}
	if err := waitForScreen(ctx, c, "ws-hello", 3*time.Second); err != nil {
		t.Fatalf("ws echo: %v\nscreen:\n%s", err, c.Snapshot().Screen)
	}
}

// waitForScreen polls until the connector's screen contains want, or times out.
func waitForScreen(ctx context.Context, c Connector, want string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if strings.Contains(c.Snapshot().Screen, want) {
			return nil
		}
		if _, err := c.Session().WaitForScreenChange(ctx, 250*time.Millisecond, -1); err != nil {
			return err
		}
	}
	if strings.Contains(c.Snapshot().Screen, want) {
		return nil
	}
	return context.DeadlineExceeded
}
