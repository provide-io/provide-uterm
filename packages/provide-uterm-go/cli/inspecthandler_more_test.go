//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

func TestActionRIDCoercion(t *testing.T) {
	if actionRID("r3") != "r3" {
		t.Fatalf("string rid = %q", actionRID("r3"))
	}
	if actionRID(float64(7)) != "7" {
		t.Fatalf("float rid = %q", actionRID(float64(7)))
	}
	if actionRID(json.Number("12")) != "12" {
		t.Fatalf("json.Number rid = %q", actionRID(json.Number("12")))
	}
	if actionRID(nil) != "" {
		t.Fatalf("nil rid = %q", actionRID(nil))
	}
	if actionRID(true) != "true" {
		t.Fatalf("bool rid = %q", actionRID(true))
	}
}

// TestForwardBadMethod covers the http.NewRequestWithContext error branch: an
// invalid HTTP method makes request construction fail, yielding a 502.
func TestForwardBadMethod(t *testing.T) {
	sess := &inspectSession{
		client: tunnelclient.NewClient("ws://127.0.0.1:1/tunnel", ""),
		gate:   tunnelclient.NewInterceptGate(30, "forward"),
		errw:   io.Discard,
	}
	rec := httptest.NewRecorder()
	sess.forward(context.Background(), rec, forwardReq{
		method: "BAD METHOD", url: "http://127.0.0.1:9/x", path: "/x", rid: "r1",
	})
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("bad method status = %d, want 502", rec.Code)
	}
}

// TestForwardRedirectAndStripHeaders covers the CheckRedirect hook (the upstream
// 3xx is returned rather than followed) and the response header-strip branch
// (content-encoding is not relayed to the client).
func TestForwardRedirectAndStripHeaders(t *testing.T) {
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Location", "/elsewhere")
		w.Header().Set("Content-Encoding", "gzip")
		w.Header().Set("X-Keep", "yes")
		w.WriteHeader(http.StatusFound)
		_, _ = io.WriteString(w, "redir")
	}))
	defer origin.Close()

	sess := &inspectSession{
		client: mustConnectDiscardTunnel(t),
		gate:   tunnelclient.NewInterceptGate(30, "forward"),
		errw:   io.Discard,
	}
	rec := httptest.NewRecorder()
	sess.forward(context.Background(), rec, forwardReq{
		method: "GET", url: origin.URL, path: "/", rid: "r1", headers: map[string]string{},
	})
	if rec.Code != http.StatusFound {
		t.Fatalf("redirect status = %d, want 302 (not followed)", rec.Code)
	}
	if rec.Header().Get("Content-Encoding") != "" {
		t.Errorf("content-encoding should be stripped, got %q", rec.Header().Get("Content-Encoding"))
	}
	if rec.Header().Get("X-Keep") != "yes" {
		t.Errorf("non-hop header should be relayed, got %q", rec.Header().Get("X-Keep"))
	}
}

// TestDecodeActionMessageInvalidBinary covers the invalid-JSON payload branch of
// decodeActionMessage for a ChannelHTTP binary frame.
func TestDecodeActionMessageInvalidBinary(t *testing.T) {
	frame := tunnelclient.EncodeFrame(tunnelclient.ChannelHTTP, []byte("not-json"), tunnelclient.FlagData)
	if _, ok := decodeActionMessage(false, frame); ok {
		t.Error("a ChannelHTTP frame with non-JSON payload must not decode")
	}
}

// TestInspectInterceptModify covers the "modify" decision branch in handle (both
// header and body substitution) plus receiveActions' skip of an undecodable
// message. The tunnel server first sends a too-short binary frame (skipped), then
// a modify action that rewrites the forwarded headers and body; the origin echoes
// them back so the substitution is observable.
//
// CI flake note: a single attempt can race the action receiver startup against the
// first GET (gate times out to "forward" with an empty body). We use a short
// intercept timeout and retry until modify is applied.
func TestInspectInterceptModify(t *testing.T) {
	targetPort := originServer(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Echo-Injected", r.Header.Get("X-Injected"))
		body, _ := io.ReadAll(r.Body)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body) // echo the (modified) request body
	})

	modBody := base64.StdEncoding.EncodeToString([]byte("MODIFIED"))
	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		for {
			_, raw, err := c.Read(ctx)
			if err != nil {
				return
			}
			ev, ok := decodeHTTPEvent(raw)
			if !ok || ev["type"] != "http_req" {
				continue
			}
			// An undecodable (too short) binary frame must be skipped by the receiver.
			// Send it *after* the modify action so a slow action receiver cannot
			// time out while stuck only seeing junk (still exercises skip path).
			action, _ := json.Marshal(map[string]any{
				"type": "http_action", "id": ev["id"], "action": "modify",
				"headers": map[string]any{"X-Injected": "1"}, "body_b64": modBody,
			})
			_ = c.Write(ctx, websocket.MessageBinary,
				tunnelclient.EncodeFrame(tunnelclient.ChannelHTTP, action, tunnelclient.FlagData))
			_ = c.Write(ctx, websocket.MessageBinary, []byte{0x01})
		}
	})

	client := tunnelclient.NewClient("ws"+strings.TrimPrefix(f.srv.URL, "http")+"/tunnel", "")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := client.Connect(ctx); err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer func() { _ = client.Close() }()

	// Short gate timeout: if a race drops the first action, retry quickly instead
	// of blocking the suite for the production default (30s).
	gate := tunnelclient.NewInterceptGate(2, "forward")
	gate.SetEnabled(true)
	sess := &inspectSession{client: client, gate: gate, targetPort: targetPort, errw: io.Discard}

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	proxyPort := ln.Addr().(*net.TCPAddr).Port
	go func() { _ = sess.serve(ctx, ln) }()

	// Wait for the proxy listener + action receiver to come up.
	deadline := time.Now().Add(8 * time.Second)
	var last httpResult
	for time.Now().Before(deadline) {
		last = tryGet(proxyPort, "/x")
		if last.status == http.StatusOK && last.body == "MODIFIED" {
			if last.headerGet("X-Echo-Injected") != "1" {
				t.Fatalf("modified header not echoed: %q", last.headerGet("X-Echo-Injected"))
			}
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	t.Fatalf("modify never applied within deadline: status=%d body=%q", last.status, last.body)
}
