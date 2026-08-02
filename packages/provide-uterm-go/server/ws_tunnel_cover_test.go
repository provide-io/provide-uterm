//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

// stubBrowserConn is a comparable identity standing in for a dashboard socket
// where only the hub's ownership bookkeeping is under test.
type stubBrowserConn struct{ name string }

func newFakeBrowserConn() *stubBrowserConn { return &stubBrowserConn{name: "stub"} }

// tunnelHarness starts a real HTTP server for the handler and returns a dialer
// for the tunnel endpoint of workerID.
type tunnelHarness struct {
	ts   *testServer
	base string
}

func newTunnelHarness(t *testing.T) *tunnelHarness {
	t.Helper()
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()
	httpSrv := httptest.NewServer(ts.srv.Handler())
	t.Cleanup(httpSrv.Close)
	return &tunnelHarness{ts: ts, base: "ws" + strings.TrimPrefix(httpSrv.URL, "http")}
}

func (h *tunnelHarness) dial(t *testing.T, ctx context.Context, workerID string) *websocket.Conn {
	t.Helper()
	conn, _, err := websocket.Dial(ctx, h.base+"/tunnel/"+workerID, nil)
	if err != nil {
		t.Fatalf("dial tunnel %s: %v", workerID, err)
	}
	return conn
}

// sawTermEvent reports whether workerID recorded a term event carrying marker.
func sawTermEvent(ts *testServer, workerID, marker string) bool {
	for _, ev := range ts.hub.GetRecentEvents(context.Background(), workerID, 200) {
		if ev["type"] != "term" {
			continue
		}
		data, _ := ev["data"].(map[string]any)
		if s, _ := data["data"].(string); strings.Contains(s, marker) {
			return true
		}
	}
	return false
}

// TestTunnelRecvLoopSkipsUndecodableFrames feeds every malformed-frame shape the
// receive loop must survive, then a valid data frame as a sentinel. WebSocket
// delivery is ordered and the receive loop is sequential, so observing the
// sentinel proves each malformed frame ahead of it was processed and dropped —
// no sleep, no scheduling assumption.
func TestTunnelRecvLoopSkipsUndecodableFrames(t *testing.T) {
	h := newTunnelHarness(t)
	h.ts.reg.add("tun-bad", "admin1", "public")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	conn := h.dial(t, ctx, "tun-bad")
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "") }()
	waitUntil(t, 5*time.Second, func() bool { return h.ts.hub.Registry.Contains("tun-bad") })

	// A frame on an unknown channel with the EOF flag set: dropped, not fatal.
	if err := conn.Write(ctx, websocket.MessageBinary, []byte{0xFE, tunnelclient.FlagEOF}); err != nil {
		t.Fatalf("write eof frame: %v", err)
	}
	// Under the two-byte minimum: dropped before decoding.
	if err := conn.Write(ctx, websocket.MessageBinary, []byte{0x01}); err != nil {
		t.Fatalf("write short frame: %v", err)
	}
	// HTTP channel carrying payload that is not JSON.
	badHTTP := tunnelclient.EncodeFrame(tunnelclient.ChannelHTTP, []byte("{not json"), tunnelclient.FlagData)
	if err := conn.Write(ctx, websocket.MessageBinary, badHTTP); err != nil {
		t.Fatalf("write bad http frame: %v", err)
	}
	// HTTP channel carrying valid JSON with no "type": the event still records,
	// under the generic "http" event type.
	untyped := tunnelclient.EncodeFrame(tunnelclient.ChannelHTTP, []byte(`{"url":"/x"}`), tunnelclient.FlagData)
	if err := conn.Write(ctx, websocket.MessageBinary, untyped); err != nil {
		t.Fatalf("write untyped http frame: %v", err)
	}
	// Control channel carrying an undecodable control payload.
	badControl := tunnelclient.EncodeFrame(tunnelclient.ChannelControl, []byte{0x00, 0x01}, tunnelclient.FlagData)
	if err := conn.Write(ctx, websocket.MessageBinary, badControl); err != nil {
		t.Fatalf("write bad control frame: %v", err)
	}
	sentinel := tunnelclient.EncodeFrame(tunnelclient.ChannelData, []byte("SENTINEL"), tunnelclient.FlagData)
	if err := conn.Write(ctx, websocket.MessageBinary, sentinel); err != nil {
		t.Fatalf("write sentinel: %v", err)
	}

	waitUntil(t, 5*time.Second, func() bool { return sawTermEvent(h.ts, "tun-bad", "SENTINEL") })
	// The untyped HTTP frame landed as a generic "http" event, proving it was
	// forwarded rather than dropped with the malformed ones.
	found := false
	for _, ev := range h.ts.hub.GetRecentEvents(ctx, "tun-bad", 200) {
		if ev["type"] == "http" {
			found = true
		}
	}
	if !found {
		t.Fatal("untyped HTTP frame did not record a generic http event")
	}
}

// TestTunnelWSRejectedWhileRestLeaseHeld proves the tunnel registration refuses a
// worker whose REST hijack lease is live, instead of silently stealing it.
func TestTunnelWSRejectedWhileRestLeaseHeld(t *testing.T) {
	h := newTunnelHarness(t)
	hijackID := acquireHijack(t, h.ts, "tun-leased")

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	conn := h.dial(t, ctx, "tun-leased")
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "") }()

	// The server accepts then closes with a policy violation so the code lands.
	_, _, err := conn.Read(ctx)
	if err == nil {
		t.Fatal("expected the tunnel socket to be closed")
	}
	if got := websocket.CloseStatus(err); got != websocket.StatusPolicyViolation {
		t.Fatalf("close status = %v, want %v", got, websocket.StatusPolicyViolation)
	}
	// The lease is untouched.
	if !h.ts.hub.CheckHijackValid("tun-leased", hijackID) {
		t.Fatal("the REST lease was disturbed by the rejected tunnel")
	}
}

// TestTunnelWSClearsPriorDashboardHijack covers the reconnect path where the
// worker was hijacked before it dropped: registering the new tunnel socket must
// clear the stale dashboard lease and announce it.
func TestTunnelWSClearsPriorDashboardHijack(t *testing.T) {
	h := newTunnelHarness(t)
	h.ts.reg.add("tun-prev", "admin1", "public")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	h.ts.setupWorker(t, "tun-prev")
	if ok, reason := h.ts.hub.TryAcquireWsHijack(ctx, "tun-prev", newFakeBrowserConn()); !ok {
		t.Fatalf("seed dashboard hijack: %s", reason)
	}

	conn := h.dial(t, ctx, "tun-prev")
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "") }()
	waitUntil(t, 5*time.Second, func() bool { return !h.ts.hub.CheckStillHijacked("tun-prev") })
}

// TestTunnelWSReleasesHijackOnDisconnect covers the mirror case: a worker that is
// hijacked when its tunnel socket drops must have the lease released and the new
// state broadcast.
func TestTunnelWSReleasesHijackOnDisconnect(t *testing.T) {
	h := newTunnelHarness(t)
	h.ts.reg.add("tun-drop", "admin1", "public")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	conn := h.dial(t, ctx, "tun-drop")
	waitUntil(t, 5*time.Second, func() bool { return h.ts.hub.Registry.Contains("tun-drop") })
	if ok, reason := h.ts.hub.TryAcquireWsHijack(ctx, "tun-drop", newFakeBrowserConn()); !ok {
		t.Fatalf("acquire dashboard hijack: %s", reason)
	}

	_ = conn.Close(websocket.StatusNormalClosure, "bye")
	waitUntil(t, 5*time.Second, func() bool { return !h.ts.hub.CheckStillHijacked("tun-drop") })
}

// TestTunnelRecvLoopStopsWhenSupersededByAnotherWorker proves a stale tunnel
// socket cannot keep injecting terminal output after another worker socket has
// taken over the same session.
func TestTunnelRecvLoopStopsWhenSupersededByAnotherWorker(t *testing.T) {
	h := newTunnelHarness(t)
	h.ts.reg.add("tun-stale", "admin1", "public")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	conn := h.dial(t, ctx, "tun-stale")
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "") }()
	waitUntil(t, 5*time.Second, func() bool { return h.ts.hub.Registry.Contains("tun-stale") })

	// A second worker socket takes the session over.
	if _, err := h.ts.hub.RegisterWorker(ctx, "tun-stale", &fakeWorkerWS{}); err != nil {
		t.Fatalf("takeover register: %v", err)
	}

	frame := tunnelclient.EncodeFrame(tunnelclient.ChannelData, []byte("STALE"), tunnelclient.FlagData)
	if err := conn.Write(ctx, websocket.MessageBinary, frame); err != nil {
		t.Fatalf("write after takeover: %v", err)
	}
	// The superseded socket is closed rather than serviced.
	for {
		if _, _, err := conn.Read(ctx); err != nil {
			break
		}
	}
	if sawTermEvent(h.ts, "tun-stale", "STALE") {
		t.Fatal("a superseded tunnel socket still injected terminal output")
	}
}
