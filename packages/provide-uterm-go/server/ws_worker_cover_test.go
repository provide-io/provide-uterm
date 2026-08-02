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

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// workerWSHarness serves the real handler and dials /ws/worker/{id}/term.
type workerWSHarness struct {
	ts   *testServer
	base string
}

func newWorkerWSHarness(t *testing.T) *workerWSHarness {
	t.Helper()
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()
	httpSrv := httptest.NewServer(ts.srv.Handler())
	t.Cleanup(httpSrv.Close)
	return &workerWSHarness{ts: ts, base: "ws" + strings.TrimPrefix(httpSrv.URL, "http")}
}

func (h *workerWSHarness) dial(t *testing.T, ctx context.Context, workerID string) *websocket.Conn {
	t.Helper()
	conn, _, err := websocket.Dial(ctx, h.base+"/ws/worker/"+workerID+"/term", nil)
	if err != nil {
		t.Fatalf("dial worker %s: %v", workerID, err)
	}
	return conn
}

// TestWorkerWSRejectedWhileRestLeaseHeld proves a worker socket cannot re-register
// underneath a live REST hijack lease: the operator holding the lease would
// otherwise silently lose the paused worker it leased.
func TestWorkerWSRejectedWhileRestLeaseHeld(t *testing.T) {
	h := newWorkerWSHarness(t)
	hijackID := acquireHijack(t, h.ts, "wk-leased")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	conn := h.dial(t, ctx, "wk-leased")
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "") }()
	_, _, err := conn.Read(ctx)
	if err == nil {
		t.Fatal("expected the worker socket to be closed")
	}
	if got := websocket.CloseStatus(err); got != websocket.StatusPolicyViolation {
		t.Fatalf("close status = %v, want %v", got, websocket.StatusPolicyViolation)
	}
	if !h.ts.hub.CheckHijackValid("wk-leased", hijackID) {
		t.Fatal("the REST lease was disturbed by the rejected worker socket")
	}
}

// TestWorkerWSClearsStaleDashboardHijackOnReconnect covers the reconnect path:
// a worker that reappears while a dashboard lease is still recorded against it
// must have that stale lease cleared and announced.
func TestWorkerWSClearsStaleDashboardHijackOnReconnect(t *testing.T) {
	h := newWorkerWSHarness(t)
	h.ts.reg.add("wk-prev", "admin1", "public")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	h.ts.setupWorker(t, "wk-prev")
	if ok, reason := h.ts.hub.TryAcquireWsHijack(ctx, "wk-prev", newFakeBrowserConn()); !ok {
		t.Fatalf("seed dashboard hijack: %s", reason)
	}

	conn := h.dial(t, ctx, "wk-prev")
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "") }()
	waitUntil(t, 5*time.Second, func() bool { return !h.ts.hub.CheckStillHijacked("wk-prev") })
}

// TestWorkerWSReleasesHijackOnDisconnect covers the mirror case: losing the
// worker socket while a dashboard lease is held must release the lease.
func TestWorkerWSReleasesHijackOnDisconnect(t *testing.T) {
	h := newWorkerWSHarness(t)
	h.ts.reg.add("wk-drop", "admin1", "public")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	conn := h.dial(t, ctx, "wk-drop")
	waitUntil(t, 5*time.Second, func() bool { return h.ts.hub.Registry.Contains("wk-drop") })
	if ok, reason := h.ts.hub.TryAcquireWsHijack(ctx, "wk-drop", newFakeBrowserConn()); !ok {
		t.Fatalf("acquire dashboard hijack: %s", reason)
	}

	_ = conn.Close(websocket.StatusNormalClosure, "bye")
	waitUntil(t, 5*time.Second, func() bool { return !h.ts.hub.CheckStillHijacked("wk-drop") })
}

// TestWorkerWSPeriodicCleanupExpiresStaleLease proves the per-connection cleanup
// loop actually runs: an expired dashboard lease is reaped while the worker
// socket stays open, with no REST call to trigger it. Expiry is driven by a
// manual clock and the assertion waits on the observable state change, so
// nothing here depends on how long a tick takes.
func TestWorkerWSPeriodicCleanupExpiresStaleLease(t *testing.T) {
	clk := hub.NewManualClock(5000)
	clk.SetMonotonic(1000)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Hub = hub.NewTermHub(hub.TermHubConfig{
			Clock: clk, OnMetric: deps.Metrics.Inc, Logger: deps.Logger,
		})
	})
	ts.hub = ts.srv.deps.Hub
	ts.srv.MarkReady()
	ts.reg.add("wk-tick", "admin1", "public")
	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	base := "ws" + strings.TrimPrefix(httpSrv.URL, "http")

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	conn, _, err := websocket.Dial(ctx, base+"/ws/worker/wk-tick/term", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "") }()
	waitUntil(t, 5*time.Second, func() bool { return ts.hub.Registry.Contains("wk-tick") })

	if ok, reason := ts.hub.TryAcquireWsHijack(ctx, "wk-tick", newFakeBrowserConn()); !ok {
		t.Fatalf("acquire dashboard hijack: %s", reason)
	}
	// Move past the lease horizon and wait for the expiry EVENT: reading the
	// lease as expired proves nothing (the comparison is against the clock), but
	// only the background cleanup pass can append hijack_owner_expired.
	clk.SetMonotonic(1_000_000)
	waitUntil(t, 10*time.Second, func() bool {
		for _, ev := range ts.hub.GetRecentEvents(context.Background(), "wk-tick", 50) {
			if ev["type"] == "hijack_owner_expired" {
				return true
			}
		}
		return false
	})
}
