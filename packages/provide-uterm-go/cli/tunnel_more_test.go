//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

// TestRunTunnelNilContext covers the nil-context guard: runTunnel defaults to
// context.Background() before registering, which then fails against a dead server.
func TestRunTunnelNilContext(t *testing.T) {
	dead := freePort(t) // freePort closes the listener, so the port is refused
	//nolint:staticcheck // deliberately passing a nil context to exercise the guard
	err := runTunnel(nil, tunnelOptions{Server: fmt.Sprintf("http://127.0.0.1:%d", dead), Port: 1}, &bytes.Buffer{})
	if err == nil {
		t.Fatal("registration against a dead server should error")
	}
}

// TestServeTunnelAcceptError covers serveTunnel's non-cancel Accept-error return:
// a pre-closed listener makes Accept fail while the context is still live.
func TestServeTunnelAcceptError(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	_ = ln.Close()
	client := tunnelclient.NewClient("ws://127.0.0.1:1/tunnel", "")
	if err := serveTunnel(context.Background(), ln, client); err == nil {
		t.Fatal("accept on a closed listener should surface an error")
	}
}

// TestRunTunnelConnectError covers the client.Connect failure branch: the server
// advertises a ws_endpoint that points at a refused port.
func TestRunTunnelConnectError(t *testing.T) {
	dead := freePort(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"tunnel_id":   "tid",
			"ws_endpoint": fmt.Sprintf("ws://127.0.0.1:%d/tunnel", dead),
		})
	}))
	defer srv.Close()
	err := runTunnel(context.Background(), tunnelOptions{Server: srv.URL, Port: freePort(t)}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "connect") {
		t.Fatalf("expected tunnel connect error, got %v", err)
	}
}

// TestRunTunnelLocalPortInUse covers the net.Listen failure branch: the tunnel
// connects successfully but the local port is already bound.
func TestRunTunnelLocalPortInUse(t *testing.T) {
	busy, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer busy.Close() //nolint:errcheck // test cleanup
	port := busy.Addr().(*net.TCPAddr).Port

	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		for {
			if _, _, rerr := c.Read(ctx); rerr != nil {
				return
			}
		}
	})
	err = runTunnel(context.Background(), tunnelOptions{Server: f.srv.URL, Port: port}, &bytes.Buffer{})
	if err == nil {
		t.Fatal("binding an in-use local port should fail")
	}
}

// TestRunTunnelServerEOFAndNonTCPChannel covers the WS→local branches: a frame on
// a non-TCP channel is skipped, and a ChannelTCP EOF frame ends the pump and
// closes the local connection.
func TestRunTunnelServerEOFAndNonTCPChannel(t *testing.T) {
	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		sentEOF := false
		for {
			_, raw, err := c.Read(ctx)
			if err != nil {
				return
			}
			frame, derr := tunnelclient.DecodeFrame(raw)
			if derr != nil {
				continue
			}
			if frame.IsControl() {
				continue // the open frame
			}
			if frame.Channel == tunnelclient.ChannelTCP && !frame.IsEOF() && !sentEOF {
				sentEOF = true
				// A frame on a non-TCP channel must be ignored by the client.
				_ = c.Write(ctx, websocket.MessageBinary,
					tunnelclient.EncodeFrame(tunnelclient.ChannelHTTP, []byte("nope"), tunnelclient.FlagData))
				// A TCP EOF frame ends the client's WS→local pump.
				_ = c.Write(ctx, websocket.MessageBinary,
					tunnelclient.EncodeFrame(tunnelclient.ChannelTCP, nil, tunnelclient.FlagEOF))
			}
		}
	})

	port := freePort(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = runTunnel(ctx, tunnelOptions{Server: f.srv.URL, Port: port}, &bytes.Buffer{}) }()

	conn := dialWithRetry(t, port)
	defer func() { _ = conn.Close() }()
	if _, err := conn.Write([]byte("hello")); err != nil {
		t.Fatalf("write: %v", err)
	}
	// The server never echoes (it only sends an ignored non-TCP frame and a TCP
	// EOF), so a bounded read must time out rather than return data.
	_ = conn.SetReadDeadline(time.Now().Add(500 * time.Millisecond))
	if n, err := conn.Read(make([]byte, 8)); err == nil {
		t.Fatalf("expected no echoed data after server EOF, read %d bytes", n)
	}
}

// TestRunTunnelSendDataErrorOnClose covers the local→WS SendData error branch: the
// tunnel WebSocket is closed while the local side keeps producing bytes, so the
// outbound framed write fails and unwinds the bridge.
func TestRunTunnelSendDataErrorOnClose(t *testing.T) {
	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		// Consume the open control frame, then drop the socket.
		_, _, _ = c.Read(ctx)
		_ = c.CloseNow()
	})

	port := freePort(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() {
		_ = runTunnel(ctx, tunnelOptions{Server: f.srv.URL, Port: port}, &bytes.Buffer{})
		close(done)
	}()

	conn := dialWithRetry(t, port)
	// Keep writing until the local side breaks; the outbound SendData must error
	// once the tunnel WS is gone.
	writeStopped := make(chan struct{})
	go func() {
		defer close(writeStopped)
		b := make([]byte, 1024)
		for {
			if _, err := conn.Write(b); err != nil {
				return
			}
			time.Sleep(5 * time.Millisecond) // keep bytes flowing without spinning a CPU
		}
	}()

	select {
	case <-writeStopped:
		// The local connection was torn down by the bridge — the target branch ran.
	case <-time.After(5 * time.Second):
		t.Fatal("local writer never observed the bridge tear-down")
	}
	_ = conn.Close()
	cancel()
	<-done
}
