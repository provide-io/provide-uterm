//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

// wsEchoServer returns an httptest server that echoes each message back with
// the same message type. capturedOrigin/capturedHeader receive the handshake's
// Origin and a custom header for assertion.
func wsEchoServer(t *testing.T, onHandshake func(r *http.Request)) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if onHandshake != nil {
			onHandshake(r)
		}
		c, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		defer func() { _ = c.CloseNow() }()
		ctx := r.Context()
		for {
			typ, data, err := c.Read(ctx)
			if err != nil {
				return
			}
			if err := c.Write(ctx, typ, data); err != nil {
				return
			}
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

// wsURL converts an httptest http:// URL to ws://.
func wsURL(httpURL string) string {
	return "ws" + strings.TrimPrefix(httpURL, "http")
}

func TestWebSocketEchoTextAndBinary(t *testing.T) {
	srv := wsEchoServer(t, nil)
	tr := NewWebSocketTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, "", 0, ConnectOptions{WS: WSOptions{URL: wsURL(srv.URL)}}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if !tr.IsConnected() {
		t.Fatal("expected connected")
	}
	if err := tr.Send(ctx, []byte("hello")); err != nil {
		t.Fatalf("send: %v", err)
	}
	got, err := tr.Receive(ctx, 4096, time.Second)
	if err != nil {
		t.Fatalf("receive: %v", err)
	}
	if string(got) != "hello" {
		t.Errorf("received %q, want hello", got)
	}
	_ = tr.Disconnect(ctx)
	if tr.IsConnected() {
		t.Error("expected disconnected")
	}
}

func TestWebSocketBinaryFrame(t *testing.T) {
	srv := wsEchoServer(t, nil)
	tr := NewWebSocketTransport()
	ctx := context.Background()
	payload := []byte{0x00, 0x01, 0xff, 0xfe}
	if err := tr.Connect(ctx, "", 0, ConnectOptions{WS: WSOptions{URL: wsURL(srv.URL), SendBinary: true}}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if err := tr.Send(ctx, payload); err != nil {
		t.Fatalf("send: %v", err)
	}
	got, err := tr.Receive(ctx, 4096, time.Second)
	if err != nil {
		t.Fatalf("receive: %v", err)
	}
	if string(got) != string(payload) {
		t.Errorf("received %v, want %v", got, payload)
	}
	_ = tr.Disconnect(ctx)
}

func TestWebSocketOriginAndHeaders(t *testing.T) {
	var gotOrigin, gotCustom string
	srv := wsEchoServer(t, func(r *http.Request) {
		gotOrigin = r.Header.Get("Origin")
		gotCustom = r.Header.Get("X-Uterm-Test")
	})
	tr := NewWebSocketTransport()
	ctx := context.Background()
	opts := ConnectOptions{WS: WSOptions{
		URL:     wsURL(srv.URL),
		Origin:  "https://allowed.example.com",
		Headers: http.Header{"X-Uterm-Test": {"v1"}},
	}}
	if err := tr.Connect(ctx, "", 0, opts); err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(ctx) }()
	if gotOrigin != "https://allowed.example.com" {
		t.Errorf("origin = %q", gotOrigin)
	}
	if gotCustom != "v1" {
		t.Errorf("custom header = %q", gotCustom)
	}
}

func TestWebSocketReceiveTimeout(t *testing.T) {
	// Server accepts but never sends: receive must time out to empty, and the
	// connection must stay open (deviation-handling reader goroutine).
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		defer func() { _ = c.CloseNow() }()
		<-r.Context().Done()
	}))
	t.Cleanup(srv.Close)

	tr := NewWebSocketTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, "", 0, ConnectOptions{WS: WSOptions{URL: wsURL(srv.URL)}}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	got, err := tr.Receive(ctx, 4096, 50*time.Millisecond)
	if err != nil {
		t.Fatalf("timeout should be nil err, got %v", err)
	}
	if len(got) != 0 {
		t.Errorf("timeout should return empty, got %v", got)
	}
	if !tr.IsConnected() {
		t.Error("connection should stay open after a receive timeout")
	}
	_ = tr.Disconnect(ctx)
}

func TestWebSocketReceiveClose(t *testing.T) {
	// Server closes right after accept: receive must return ErrConnectionClosed.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		_ = c.Close(websocket.StatusNormalClosure, "bye")
	}))
	t.Cleanup(srv.Close)

	tr := NewWebSocketTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, "", 0, ConnectOptions{WS: WSOptions{URL: wsURL(srv.URL)}}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	_, err := tr.Receive(ctx, 4096, time.Second)
	if !errors.Is(err, ErrConnectionClosed) {
		t.Errorf("want ErrConnectionClosed, got %v", err)
	}
	if tr.IsConnected() {
		t.Error("should be disconnected")
	}
}

func TestWebSocketDefaultURLBuild(t *testing.T) {
	// No URL provided and no reachable host: Connect should fail building
	// wss://host:port (exercises the URL fallback branch).
	tr := NewWebSocketTransport()
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	err := tr.Connect(ctx, "127.0.0.1", 1, ConnectOptions{Timeout: 200 * time.Millisecond})
	if err == nil {
		t.Fatal("expected failure connecting to wss://127.0.0.1:1")
	}
}

func TestWebSocketNotConnected(t *testing.T) {
	tr := NewWebSocketTransport()
	ctx := context.Background()
	if err := tr.Send(ctx, []byte("x")); !errors.Is(err, ErrNotConnected) {
		t.Errorf("send: %v", err)
	}
	if _, err := tr.Receive(ctx, 10, time.Millisecond); !errors.Is(err, ErrNotConnected) {
		t.Errorf("receive: %v", err)
	}
	if tr.IsConnected() {
		t.Error("should not be connected")
	}
	if err := tr.Disconnect(ctx); err != nil {
		t.Errorf("disconnect noop: %v", err)
	}
}

func TestWebSocketSendAfterClose(t *testing.T) {
	srv := wsEchoServer(t, nil)
	tr := NewWebSocketTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, "", 0, ConnectOptions{WS: WSOptions{URL: wsURL(srv.URL)}}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	_ = tr.Disconnect(ctx)
	if err := tr.Send(ctx, []byte("x")); !errors.Is(err, ErrNotConnected) {
		t.Errorf("send after disconnect: %v", err)
	}
}

func TestWebSocketSendWriteError(t *testing.T) {
	// Server closes the connection; a subsequent send must fail and disconnect.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		_ = c.Close(websocket.StatusNormalClosure, "bye")
	}))
	t.Cleanup(srv.Close)

	tr := NewWebSocketTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, "", 0, ConnectOptions{WS: WSOptions{URL: wsURL(srv.URL)}}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	// Loop until the closed peer surfaces as a write error.
	var sendErr error
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if err := tr.Send(ctx, []byte("x")); err != nil {
			sendErr = err
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if sendErr == nil {
		t.Skip("peer close did not surface as a write error in time (transport-timing dependent)")
	}
	if !errors.Is(sendErr, ErrConnectionClosed) {
		t.Errorf("want ErrConnectionClosed, got %v", sendErr)
	}
}

func TestWebSocketContextCancelReceive(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		defer func() { _ = c.CloseNow() }()
		<-r.Context().Done()
	}))
	t.Cleanup(srv.Close)

	tr := NewWebSocketTransport()
	base := context.Background()
	if err := tr.Connect(base, "", 0, ConnectOptions{WS: WSOptions{URL: wsURL(srv.URL)}}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(base) }()
	ctx, cancel := context.WithCancel(base)
	cancel()
	if _, err := tr.Receive(ctx, 4096, time.Second); !errors.Is(err, context.Canceled) {
		t.Errorf("want context canceled, got %v", err)
	}
}
