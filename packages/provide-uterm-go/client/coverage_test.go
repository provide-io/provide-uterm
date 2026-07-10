//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package client

import (
	"context"
	"net/http"
	"testing"
	"time"

	"github.com/coder/websocket"
)

// TestWithTimeoutApplied wires a 1ms default timeout against a server that
// sleeps far longer, forcing a context-deadline transport error — proving
// WithTimeout is threaded into the per-request context.
func TestWithTimeoutApplied(t *testing.T) {
	fs := newFakeServer(t)
	fs.srv.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(200 * time.Millisecond)
		_, _ = w.Write([]byte("{}"))
	})
	c := fs.client(WithTimeout(1 * time.Millisecond))
	_, err := c.Health(ctx())
	apiErr, ok := err.(*APIError)
	if !ok || !apiErr.Transport {
		t.Fatalf("expected transport timeout, got %v", err)
	}
}

// TestMarshalErrorIsTransport covers the json.Marshal failure branch in
// doRequest: a channel value cannot be encoded.
func TestMarshalErrorIsTransport(t *testing.T) {
	c := NewHijackClient("http://test")
	_, err := c.Post(ctx(), "/api/x", map[string]any{"bad": make(chan int)})
	apiErr, ok := err.(*APIError)
	if !ok || !apiErr.Transport {
		t.Fatalf("expected transport marshal error, got %v", err)
	}
}

// TestBuildURLParseError covers the url.Parse failure branch: a control
// character in the base URL makes parsing fail before any request is made.
func TestBuildURLParseError(t *testing.T) {
	c := NewHijackClient("http://\x7fbad")
	_, err := c.Health(ctx())
	apiErr, ok := err.(*APIError)
	if !ok || !apiErr.Transport {
		t.Fatalf("expected transport parse error, got %v", err)
	}
}

// TestRequestObjectNonMapDefensive covers the defensive branch where an object
// endpoint returns a non-object body.
func TestRequestObjectNonMapDefensive(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("GET", "/api/health", fakeResponse{status: 200, body: []any{1, 2}})
	c := fs.client()
	data, err := c.Health(ctx())
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := data["raw"]; !ok {
		t.Fatalf("non-object body should be wrapped under raw: %v", data)
	}
}

// TestExtractErrorArrayBody covers extractError's json.Marshal fallback when
// the failure body is not a map with an "error" string.
func TestExtractErrorArrayBody(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("GET", "/api/sessions", fakeResponse{status: 500, body: []any{1, 2, 3}})
	c := fs.client()
	_, err := c.ListSessions(ctx())
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("not APIError: %T", err)
	}
	if apiErr.Message != "[1,2,3]" {
		t.Fatalf("extractError array: %q", apiErr.Message)
	}
}

// TestReadBodyError covers the io.ReadAll failure branch in doRequest: the
// server promises a longer body via Content-Length than it delivers, then
// hijacks and closes the connection, so reading the body errors.
func TestReadBodyError(t *testing.T) {
	fs := newFakeServer(t)
	fs.srv.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hj, ok := w.(http.Hijacker)
		if !ok {
			t.Fatal("no hijacker")
		}
		conn, buf, err := hj.Hijack()
		if err != nil {
			t.Fatal(err)
		}
		// Claim 100 body bytes but write only 3, then close abruptly.
		_, _ = buf.WriteString("HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nabc")
		_ = buf.Flush()
		_ = conn.Close()
	})
	// Disable keep-alive retries so the read error surfaces directly.
	c := fs.client(WithHTTPClient(&http.Client{Transport: &http.Transport{DisableKeepAlives: true}}))
	_, err := c.Health(ctx())
	apiErr, ok := err.(*APIError)
	if !ok || !apiErr.Transport {
		t.Fatalf("expected transport read error, got %v", err)
	}
}

// TestRecvFrameDecoderError covers the decoder-error branch in RecvFrame: the
// server sends a text message that is a malformed control frame.
func TestRecvFrameDecoderError(t *testing.T) {
	hub := newWSHub(t, func(ctx context.Context, conn *websocket.Conn) {
		// DLE followed by a non-DLE/non-STX byte → invalid control prefix.
		_ = conn.Write(ctx, websocket.MessageText, []byte("\x10X"))
		_, _, _ = conn.Read(ctx)
	})
	c, err := Dial(ctx(), hub.wsURL("/ws/browser/x"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close(websocket.StatusNormalClosure, "") }()
	if _, err := c.RecvFrame(ctx()); err == nil {
		t.Fatal("expected decoder protocol error")
	}
}
