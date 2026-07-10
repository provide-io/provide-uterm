//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnelclient

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

// wsEcho starts an httptest WebSocket server. The handler receives the accepted
// connection and the Authorization header seen at upgrade. It replaces http/https
// in the returned URL with ws/wss.
func wsServer(t *testing.T, handler func(ctx context.Context, c *websocket.Conn, authz string)) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer c.CloseNow() //nolint:errcheck // test cleanup
		handler(r.Context(), c, r.Header.Get("Authorization"))
	}))
	t.Cleanup(srv.Close)
	return "ws" + strings.TrimPrefix(srv.URL, "http")
}

func TestClientConnectSendRecv(t *testing.T) {
	gotAuth := make(chan string, 1)
	url := wsServer(t, func(ctx context.Context, c *websocket.Conn, authz string) {
		gotAuth <- authz
		// Echo the first frame back.
		_, data, err := c.Read(ctx)
		if err != nil {
			return
		}
		_ = c.Write(ctx, websocket.MessageBinary, data)
		// Also send a text control frame to exercise Recv's text path.
		_ = c.Write(ctx, websocket.MessageText, EncodeFrame(ChannelControl, []byte(`{"type":"hi"}`), FlagData))
		time.Sleep(50 * time.Millisecond)
	})

	c := NewClient(url, "tok123")
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := c.Connect(ctx); err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer func() { _ = c.Close() }()
	if !c.Connected() {
		t.Fatal("should be connected")
	}
	if authz := <-gotAuth; authz != "Bearer tok123" {
		t.Fatalf("authz header = %q", authz)
	}

	if err := c.SendData(ctx, []byte("payload"), ChannelData); err != nil {
		t.Fatalf("send: %v", err)
	}
	frame, err := c.Recv(ctx)
	if err != nil {
		t.Fatalf("recv: %v", err)
	}
	if frame.Channel != ChannelData || string(frame.Payload) != "payload" {
		t.Fatalf("echoed frame = %+v", frame)
	}
	// Text control frame decodes the same way.
	ctrl, err := c.Recv(ctx)
	if err != nil {
		t.Fatalf("recv control: %v", err)
	}
	if !ctrl.IsControl() {
		t.Fatalf("expected control frame, got channel %d", ctrl.Channel)
	}
}

func TestClientControlAndResizeAndEOF(t *testing.T) {
	frames := make(chan []byte, 8)
	url := wsServer(t, func(ctx context.Context, c *websocket.Conn, _ string) {
		for {
			_, data, err := c.Read(ctx)
			if err != nil {
				return
			}
			frames <- data
		}
	})
	c := NewClient(url, "")
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := c.Connect(ctx); err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer func() { _ = c.Close() }()

	if err := c.OpenTerminal(ctx, 80, 24); err != nil {
		t.Fatalf("open terminal: %v", err)
	}
	if err := c.SendResize(ctx, 100, 40); err != nil {
		t.Fatalf("resize: %v", err)
	}
	if err := c.SendControl(ctx, map[string]any{"type": "ping"}); err != nil {
		t.Fatalf("control: %v", err)
	}
	if err := c.SendEOF(ctx, ChannelData); err != nil {
		t.Fatalf("eof: %v", err)
	}
	// RecvMessage / RecvRaw smoke: connection remains usable.
	raw := <-frames
	f, _ := DecodeFrame(raw)
	if !f.IsControl() {
		t.Fatalf("first frame should be control (open terminal), got %d", f.Channel)
	}
	<-frames // resize
	<-frames // ping
	eof := <-frames
	ef, _ := DecodeFrame(eof)
	if !ef.IsEOF() {
		t.Fatal("last frame should be EOF")
	}

	// SendControl without type must error.
	if err := c.SendControl(ctx, map[string]any{"no": "type"}); err == nil {
		t.Fatal("control without type should error")
	}
}

func TestClientNotConnected(t *testing.T) {
	c := NewClient("ws://127.0.0.1:1", "")
	ctx := context.Background()
	if c.Connected() {
		t.Fatal("should not be connected")
	}
	if err := c.SendData(ctx, []byte("x"), ChannelData); err != ErrNotConnected {
		t.Fatalf("SendData err = %v", err)
	}
	if err := c.SendEOF(ctx, ChannelData); err != ErrNotConnected {
		t.Fatalf("SendEOF err = %v", err)
	}
	if _, err := c.Recv(ctx); err != ErrNotConnected {
		t.Fatalf("Recv err = %v", err)
	}
	if _, err := c.RecvRaw(ctx); err != ErrNotConnected {
		t.Fatalf("RecvRaw err = %v", err)
	}
	if _, _, err := c.RecvMessage(ctx); err != ErrNotConnected {
		t.Fatalf("RecvMessage err = %v", err)
	}
	if err := c.SendControl(ctx, map[string]any{"type": "x"}); err != ErrNotConnected {
		t.Fatalf("SendControl err = %v", err)
	}
	// Close is idempotent when never connected.
	if err := c.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
}

func TestClientConnectFailure(t *testing.T) {
	c := NewClient("ws://127.0.0.1:1", "tok")
	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	if err := c.Connect(ctx); err == nil {
		t.Fatal("expected connect failure to dead port")
	}
}

func TestReconnectLoopCancels(t *testing.T) {
	c := NewClient("ws://127.0.0.1:1", "")
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // cancel immediately: the first backoff sleep returns ctx.Err()
	if err := c.ReconnectLoop(ctx, 3); err == nil {
		t.Fatal("expected ctx cancellation error")
	}
}

func TestReconnectLoopSucceeds(t *testing.T) {
	url := wsServer(t, func(ctx context.Context, c *websocket.Conn, _ string) {
		time.Sleep(20 * time.Millisecond)
	})
	c := NewClient(url, "")
	// backoffSchedule[0] is 1s; keep the test bounded but real.
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := c.ReconnectLoop(ctx, 1); err != nil {
		t.Fatalf("reconnect: %v", err)
	}
	if !c.Connected() {
		t.Fatal("should be connected after reconnect")
	}
	_ = c.Close()
}
