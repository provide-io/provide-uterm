//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// wsServer starts an httptest server for the given testServer and returns the
// ws:// base URL.
func wsServer(t *testing.T, ts *testServer) (string, func()) {
	t.Helper()
	ts.srv.MarkReady()
	httpSrv := httptest.NewServer(ts.srv.Handler())
	return "ws" + strings.TrimPrefix(httpSrv.URL, "http"), httpSrv.Close
}

// TestBrowserWSInvalidID covers the validID rejection (HTTP 404, no upgrade).
func TestBrowserWSInvalidID(t *testing.T) {
	ts := newTestServer(t, nil)
	if rec := ts.do("GET", "/ws/browser/bad!id/term", "", nil); rec.Code != http.StatusNotFound {
		t.Fatalf("invalid browser id: %d", rec.Code)
	}
}

// TestWSAcceptFailureNonUpgrade covers the websocket.Accept failure return in
// both handlers (a plain GET with a valid id is not an upgrade).
func TestWSAcceptFailureNonUpgrade(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("noup", "admin1", "public")
	// Neither is a 404/200 upgrade; Accept fails and the handler returns. We
	// only need the handler to run its Accept-error path without panicking.
	_ = ts.do("GET", "/ws/worker/noup/term", "", adminHeaders())
	_ = ts.do("GET", "/ws/browser/noup/term", "", adminHeaders())
}

// TestBrowserWSAnonymousRejected covers the anonymous-principal 1008 close.
func TestBrowserWSAnonymousRejected(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("anon", "admin1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// No X-Subject header → anonymous → server accepts then closes 1008.
	conn, _, err := websocket.Dial(ctx, base+"/ws/browser/anon/term", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "") }()
	if _, _, rerr := conn.Read(ctx); rerr == nil {
		t.Fatal("expected close for anonymous browser")
	}
	if ts.metrics.Snapshot()["auth_failures_ws_total"] < 1 {
		t.Fatal("auth_failures_ws_total not counted")
	}
}

// TestBrowserWSInsufficientRole covers resolveBrowserRole's CanReadSession
// denial (a viewer on a private session they cannot read).
func TestBrowserWSInsufficientRole(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("priv", "admin1", "private")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn, _, err := websocket.Dial(ctx, base+"/ws/browser/priv/term", &websocket.DialOptions{
		HTTPHeader: http.Header{"X-Subject": {"view1"}, "X-Role": {"viewer"}},
	})
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "") }()
	if _, _, rerr := conn.Read(ctx); rerr == nil {
		t.Fatal("expected close for insufficient role")
	}
}

// TestBrowserWSBinaryAndUnknownFrames drives the binary-message DataChunk path
// and the unknown-message default branch.
func TestBrowserWSBinaryAndUnknownFrames(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bin", "admin1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	bc := dialBrowser(t, ctx, base+"/ws/browser/bin/term", "admin1", "admin")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.waitFrame(t, "hello", 5*time.Second)

	// Binary WS message with raw (non-control) bytes → decoder yields a
	// DataChunk → treated as browser input.
	if err := bc.conn.Write(ctx, websocket.MessageBinary, []byte("rawbytes")); err != nil {
		t.Fatalf("binary write: %v", err)
	}
	// Unknown control type → default (dropped) branch.
	bc.send(t, ctx, map[string]any{"type": "totally_unknown"})
	// Liveness check: a ping still round-trips.
	bc.send(t, ctx, map[string]any{"type": "ping"})
	bc.waitFrame(t, "pong", 5*time.Second)
}

// TestBrowserWSNonOwnerHeartbeat covers the heartbeat non-owner (nil lease)
// branch: a viewer's heartbeat is ignored (no ack), but the socket stays live.
func TestBrowserWSNonOwnerHeartbeat(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("hb", "admin1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	vc := dialBrowser(t, ctx, base+"/ws/browser/hb/term", "view1", "viewer")
	defer func() { _ = vc.conn.Close(websocket.StatusNormalClosure, "") }()
	vc.waitFrame(t, "hello", 5*time.Second)
	// A viewer owns no lease → heartbeat yields no ack; a subsequent ping does.
	vc.send(t, ctx, map[string]any{"type": "heartbeat"})
	vc.send(t, ctx, map[string]any{"type": "ping"})
	vc.waitFrame(t, "pong", 5*time.Second)
}

// TestBrowserWSDisconnectWhileOwner covers browserCleanup's was-owner resume
// path: an admin acquires the WS hijack then drops the socket without releasing.
func TestBrowserWSDisconnectWhileOwner(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("own", "admin1", "public")
	worker := ts.setupWorker(t, "own")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	bc := dialBrowser(t, ctx, base+"/ws/browser/own/term", "admin1", "admin")
	bc.waitFrame(t, "hello", 5*time.Second)
	bc.send(t, ctx, map[string]any{"type": "hijack_request"})
	bc.waitFrameWhere(t, "hijack_state", 5*time.Second, func(f map[string]any) bool {
		return f["hijacked"] == true
	})
	// Drop the socket abruptly — the recv loop ends with owned=true, so
	// browserCleanup runs the was-owner resume/notify sequence.
	_ = bc.conn.Close(websocket.StatusAbnormalClosure, "bye")
	// The worker eventually receives a resume control frame from cleanup.
	waitUntil(t, 5*time.Second, func() bool {
		for _, p := range append([]string(nil), workerSent(worker)...) {
			if strings.Contains(p, "resume") {
				return true
			}
		}
		return false
	})
}

// TestBrowserWSAlreadyHijacked covers the already_hijacked acquire failure (a
// second admin requesting the lease) — no resume is sent for that reason.
func TestBrowserWSAlreadyHijacked(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("dup", "admin1", "public")
	ts.setupWorker(t, "dup")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	b1 := dialBrowser(t, ctx, base+"/ws/browser/dup/term", "admin1", "admin")
	defer func() { _ = b1.conn.Close(websocket.StatusNormalClosure, "") }()
	b1.waitFrame(t, "hello", 5*time.Second)
	b1.send(t, ctx, map[string]any{"type": "hijack_request"})
	b1.waitFrameWhere(t, "hijack_state", 5*time.Second, func(f map[string]any) bool {
		return f["hijacked"] == true
	})

	b2 := dialBrowser(t, ctx, base+"/ws/browser/dup/term", "admin2", "admin")
	defer func() { _ = b2.conn.Close(websocket.StatusNormalClosure, "") }()
	b2.waitFrame(t, "hello", 5*time.Second)
	b2.send(t, ctx, map[string]any{"type": "hijack_request"})
	b2.waitFrameWhere(t, "error", 5*time.Second, func(f map[string]any) bool {
		msg, _ := f["message"].(string)
		return strings.Contains(msg, "Already hijacked")
	})
}

// TestBrowserWSHijackNoWorker covers the acquire-failure resume branch (reason
// != already_hijacked): with no worker registered, a hijack_request fails
// no_worker and the handler sends a resume + "No worker connected" error.
func TestBrowserWSHijackNoWorker(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("noworker", "admin1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	bc := dialBrowser(t, ctx, base+"/ws/browser/noworker/term", "admin1", "admin")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.waitFrame(t, "hello", 5*time.Second)
	bc.send(t, ctx, map[string]any{"type": "hijack_request"})
	bc.waitFrameWhere(t, "error", 5*time.Second, func(f map[string]any) bool {
		msg, _ := f["message"].(string)
		return strings.Contains(msg, "No worker connected")
	})
}

// TestWorkerWSBinaryAndControlFrames covers the worker recv-loop binary path
// plus the analysis + unknown control dispatch branches, exercised over a live
// worker socket.
func TestWorkerWSBinaryAndControlFrames(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("wctl", "admin1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	conn, _, err := websocket.Dial(ctx, base+"/ws/worker/wctl/term", nil)
	if err != nil {
		t.Fatalf("worker dial: %v", err)
	}
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "") }()
	waitUntil(t, 5*time.Second, func() bool { return ts.hub.Registry.Contains("wctl") })

	// A browser observer deterministically confirms the worker frames were
	// dispatched (the analysis frame is broadcast to it).
	bc := dialBrowser(t, ctx, base+"/ws/browser/wctl/term", "admin1", "admin")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.waitFrame(t, "hello", 5*time.Second)

	// Binary terminal data → DataChunk broadcast path.
	if err := conn.Write(ctx, websocket.MessageBinary, []byte("term-output")); err != nil {
		t.Fatalf("binary: %v", err)
	}
	// analysis + status + an unknown control type.
	for _, msg := range []map[string]any{
		{"type": "analysis", "data": map[string]any{"x": 1}},
		{"type": "status", "state": "running"},
		{"type": "totally_unknown_control"},
	} {
		payload, encErr := controlchannel.EncodeControlFrame(msg)
		if encErr != nil {
			t.Fatalf("encode: %v", encErr)
		}
		if err := conn.Write(ctx, websocket.MessageText, []byte(payload)); err != nil {
			t.Fatalf("write control: %v", err)
		}
	}
	// The browser receives the broadcast term data + analysis, proving both the
	// binary DataChunk path and the analysis dispatch branch executed.
	waitUntil(t, 5*time.Second, func() bool {
		select {
		case d := <-bc.data:
			return strings.Contains(d, "term-output")
		case <-time.After(100 * time.Millisecond):
			return false
		}
	})
	bc.waitFrame(t, "analysis", 5*time.Second)
}

// TestWorkerWSSuperseded covers the IsActiveWorker-false close: a second worker
// registration for the same id supersedes the first, whose recv loop then
// closes on its next read.
func TestWorkerWSSuperseded(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("sup", "admin1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	c1, _, err := websocket.Dial(ctx, base+"/ws/worker/sup/term", nil)
	if err != nil {
		t.Fatalf("dial c1: %v", err)
	}
	defer func() { _ = c1.Close(websocket.StatusNormalClosure, "") }()
	waitUntil(t, 5*time.Second, func() bool { return ts.hub.Registry.Contains("sup") })

	// Second registration supersedes the first.
	c2, _, err := websocket.Dial(ctx, base+"/ws/worker/sup/term", nil)
	if err != nil {
		t.Fatalf("dial c2: %v", err)
	}
	defer func() { _ = c2.Close(websocket.StatusNormalClosure, "") }()

	// The first socket, on its next inbound frame, detects it is no longer the
	// active worker and closes. Send a frame then expect the read side to end.
	waitUntil(t, 5*time.Second, func() bool {
		_ = c1.Write(ctx, websocket.MessageBinary, []byte("x"))
		rctx, rcancel := context.WithTimeout(ctx, 200*time.Millisecond)
		defer rcancel()
		_, _, rerr := c1.Read(rctx)
		return rerr != nil && rctx.Err() == nil
	})
}

// workerSent returns a snapshot of the payloads recorded by a fakeWorkerWS.
func workerSent(w *fakeWorkerWS) []string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]string(nil), w.sent...)
}
