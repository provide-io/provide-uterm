//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

func TestRunTunnelEchoRoundTrip(t *testing.T) {
	gotOpen := make(chan tunnelclient.Frame, 1)
	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		sawOpen := false
		for {
			_, raw, err := c.Read(ctx)
			if err != nil {
				return
			}
			frame, derr := tunnelclient.DecodeFrame(raw)
			if derr != nil {
				continue
			}
			if !sawOpen && frame.IsControl() {
				sawOpen = true
				gotOpen <- frame
				continue
			}
			if frame.Channel == tunnelclient.ChannelTCP && !frame.IsEOF() {
				// echo back on ChannelTCP
				_ = c.Write(ctx, websocket.MessageBinary,
					tunnelclient.EncodeFrame(tunnelclient.ChannelTCP, frame.Payload, tunnelclient.FlagData))
			}
		}
	})

	port := freePort(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var out bytes.Buffer
	runErr := make(chan error, 1)
	go func() {
		runErr <- runTunnel(ctx, tunnelOptions{Server: f.srv.URL, Port: port}, &out)
	}()

	// Wait for the local listener to come up, then round-trip through the tunnel.
	conn := dialWithRetry(t, port)
	defer func() { _ = conn.Close() }()
	if _, err := conn.Write([]byte("ping")); err != nil {
		t.Fatalf("write: %v", err)
	}
	buf := make([]byte, 16)
	_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	n, err := conn.Read(buf)
	if err != nil {
		t.Fatalf("read echo: %v", err)
	}
	if string(buf[:n]) != "ping" {
		t.Fatalf("echo = %q, want ping", buf[:n])
	}

	// The open control frame must name the TCP channel + local port.
	select {
	case fr := <-gotOpen:
		ctrl, _ := tunnelclient.DecodeControl(fr.Payload)
		if ctrl["type"] != "open" || ctrl["tunnel_type"] != "tcp" {
			t.Fatalf("open frame = %v", ctrl)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("never saw open control frame")
	}

	// Registration body carried the local port.
	if body, _ := f.firstReg(); body["local_port"] != float64(port) {
		t.Fatalf("registration local_port = %v, want %d", body["local_port"], port)
	}
	if !strings.Contains(out.String(), "Tunneling localhost:") {
		t.Fatalf("banner missing: %q", out.String())
	}

	_ = conn.Close()
	cancel()
	select {
	case err := <-runErr:
		if err != nil {
			t.Fatalf("runTunnel: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("runTunnel did not return after cancel")
	}
}

func dialWithRetry(t *testing.T, port int) net.Conn {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		conn, err := net.Dial("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
		if err == nil {
			return conn
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("could not dial local tunnel port %d", port)
	return nil
}

func TestRunTunnelMissingWSEndpoint(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"share_url": "x"})
	}))
	defer srv.Close()
	err := runTunnel(context.Background(), tunnelOptions{Server: srv.URL, Port: 12345}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "ws_endpoint") {
		t.Fatalf("expected ws_endpoint error, got %v", err)
	}
}

func TestRunTunnelRegistrationError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "bad", http.StatusBadRequest)
	}))
	defer srv.Close()
	err := runTunnel(context.Background(), tunnelOptions{Server: srv.URL, Port: 1}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "HTTP 400") {
		t.Fatalf("expected HTTP 400, got %v", err)
	}
}

func TestTunnelArgParseError(t *testing.T) {
	var out, errw bytes.Buffer
	if code := Execute([]string{"tunnel", "notaport", "-s", "http://x"}, &out, &errw); code == 0 {
		t.Fatal("non-integer PORT should fail")
	}
}
