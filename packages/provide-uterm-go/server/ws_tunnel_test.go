//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

// TestTunnelWSHappyPath exercises handleTunnelWS + tunnelRecvLoop control/HTTP/data paths.
func TestTunnelWSHappyPath(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()
	ts.reg.add("tun1", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsURL := "ws" + strings.TrimPrefix(httpSrv.URL, "http") + "/tunnel/tun1"

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	conn, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		t.Fatalf("dial tunnel: %v", err)
	}
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "") }()

	waitUntil(t, 3*time.Second, func() bool {
		return ts.hub.Registry.Contains("tun1")
	})

	httpPayload, _ := json.Marshal(map[string]any{"type": "http_request", "method": "GET", "url": "/"})
	httpFrame := tunnelclient.EncodeFrame(tunnelclient.ChannelHTTP, httpPayload, tunnelclient.FlagData)
	if err := conn.Write(ctx, websocket.MessageBinary, httpFrame); err != nil {
		t.Fatalf("write http frame: %v", err)
	}

	dataFrame := tunnelclient.EncodeFrame(tunnelclient.ChannelData, []byte("hello-term"), tunnelclient.FlagData)
	if err := conn.Write(ctx, websocket.MessageBinary, dataFrame); err != nil {
		t.Fatalf("write data frame: %v", err)
	}

	openFrame, err := tunnelclient.EncodeControl(map[string]any{
		"type": "open", "input_mode": "open",
	})
	if err != nil {
		t.Fatalf("encode open: %v", err)
	}
	if err := conn.Write(ctx, websocket.MessageBinary, openFrame); err != nil {
		t.Fatalf("write open: %v", err)
	}
	snapFrame, err := tunnelclient.EncodeControl(map[string]any{
		"type": "snapshot", "screen": "SCRN",
	})
	if err != nil {
		t.Fatalf("encode snap: %v", err)
	}
	if err := conn.Write(ctx, websocket.MessageBinary, snapFrame); err != nil {
		t.Fatalf("write snap: %v", err)
	}

	// short / invalid frames ignored
	_ = conn.Write(ctx, websocket.MessageBinary, []byte{0x00})
	_ = conn.Write(ctx, websocket.MessageText, []byte("not-a-frame"))

	waitUntil(t, 2*time.Second, func() bool {
		st := ts.hub.Registry.Get("tun1")
		return st != nil && st.WorkerWS != nil
	})
	st := ts.hub.Registry.Get("tun1")
	if sender, ok := st.WorkerWS.(hub.TunnelSender); ok {
		if err := sender.SendInput(ctx, "typed"); err != nil {
			t.Fatalf("SendInput: %v", err)
		}
		if err := sender.SendHTTPControl(ctx, map[string]any{"type": "action", "id": "1"}); err != nil {
			t.Fatalf("SendHTTPControl: %v", err)
		}
	} else {
		t.Fatalf("worker is not TunnelSender: %T", st.WorkerWS)
	}

	_ = conn.Close(websocket.StatusNormalClosure, "done")
	waitUntil(t, 3*time.Second, func() bool {
		st := ts.hub.Registry.Get("tun1")
		return st == nil || st.WorkerWS == nil
	})
}

// TestTunnelWSAuthAndValidation covers invalid id, non-WS GET, and worker bearer auth.
func TestTunnelWSAuthAndValidation(t *testing.T) {
	tok := "worker-sekret"
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		h := hub.NewTermHub(hub.TermHubConfig{WorkerToken: &tok})
		deps.Hub = h
	})
	ts.hub = ts.srv.deps.Hub
	ts.srv.MarkReady()

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	base := "ws" + strings.TrimPrefix(httpSrv.URL, "http")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// Invalid id
	_, resp, err := websocket.Dial(ctx, base+"/tunnel/bad!id", nil)
	if err == nil {
		t.Fatal("expected dial failure for invalid id")
	}
	if resp != nil {
		t.Logf("invalid id status: %v", resp.StatusCode)
	}

	// Non-WS GET
	rec := ts.do("GET", "/tunnel/tun-auth", "", nil)
	if rec.Code == http.StatusSwitchingProtocols {
		t.Fatalf("GET without upgrade should not switch protocols: %d", rec.Code)
	}

	// Wrong bearer
	_, _, _ = websocket.Dial(ctx, base+"/tunnel/tun-auth", &websocket.DialOptions{
		HTTPHeader: http.Header{"Authorization": []string{"Bearer wrong"}},
	})

	// Correct bearer
	conn, _, err := websocket.Dial(ctx, base+"/tunnel/tun-auth", &websocket.DialOptions{
		HTTPHeader: http.Header{"Authorization": []string{"Bearer " + tok}},
	})
	if err != nil {
		t.Fatalf("dial with token: %v", err)
	}
	_ = conn.Close(websocket.StatusNormalClosure, "")
}
