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

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/fanout"
)

// TestWSBrowserFanoutSendRouted drives a real browser WebSocket through the
// fanout_send dispatch path: an owned group yields a decoded fanout_result
// frame, and an unowned group is silently ignored (no frame, socket stays live).
func TestWSBrowserFanoutSendRouted(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()
	ts.reg.add("fo", "admin1", "public")

	// A group owned by admin1 (the browser principal) with two unconnected
	// workers, so Send resolves quickly with all-failed results.
	if _, err := ts.srv.fanout.CreateGroup(&fanout.Group{
		GroupID: "grp", Name: "g", WorkerIDs: []string{"wx", "wy"},
		Mode: "parallel", QuiesceMS: 20, MaxResponseMS: 50, DivergenceThreshold: 0.8,
	}, "admin1"); err != nil {
		t.Fatalf("create owned group: %v", err)
	}
	// A group owned by someone else — admin1 must not be able to fan-out to it.
	if _, err := ts.srv.fanout.CreateGroup(&fanout.Group{
		GroupID: "other", Name: "o", WorkerIDs: []string{"wz"},
		Mode: "parallel", QuiesceMS: 20, MaxResponseMS: 50, DivergenceThreshold: 0.8,
	}, "bob"); err != nil {
		t.Fatalf("create other group: %v", err)
	}

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	bc := dialBrowser(t, ctx, wsBase+"/ws/browser/fo/term", "admin1", "admin")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.waitFrame(t, "hello", 5*time.Second)

	// Owned group → fanout_result with the two failed workers.
	bc.send(t, ctx, map[string]any{"type": "fanout_send", "group_id": "grp", "data": "uptime\n"})
	res := bc.waitFrame(t, "fanout_result", 5*time.Second)
	if res["group_id"] != "grp" {
		t.Fatalf("fanout_result group_id = %v", res["group_id"])
	}
	failed := res["failed_sessions"].([]any)
	if len(failed) != 2 {
		t.Fatalf("failed_sessions = %v, want 2", failed)
	}
	results := res["results"].([]any)
	if len(results) != 2 {
		t.Fatalf("results len = %d, want 2", len(results))
	}

	// Unowned group → NO fanout_result. Send it, then a ping; a pong must arrive
	// with no intervening fanout_result frame.
	bc.send(t, ctx, map[string]any{"type": "fanout_send", "group_id": "other", "data": "rm\n"})
	bc.send(t, ctx, map[string]any{"type": "ping"})
	deadline := time.After(5 * time.Second)
	for {
		select {
		case f := <-bc.frames:
			if f["type"] == "fanout_result" {
				t.Fatal("unowned fanout_send must not produce a fanout_result")
			}
			if f["type"] == "pong" {
				return // socket alive, no fanout_result seen
			}
		case <-deadline:
			t.Fatal("timed out waiting for pong after unowned fanout_send")
		}
	}
}

func TestWSBrowserFanoutSendRequiresGlobalAdminWithoutWorkerInput(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()
	ts.reg.add("fo-view", "scoped1", "public")
	worker := ts.setupWorker(t, "fanout-target")
	if _, err := ts.srv.fanout.CreateGroup(&fanout.Group{
		GroupID: "viewer-group", Name: "g", WorkerIDs: []string{"fanout-target"},
		Mode: "parallel", QuiesceMS: 20, MaxResponseMS: 50, DivergenceThreshold: 0.8,
	}, "scoped1"); err != nil {
		t.Fatalf("create scoped-admin group: %v", err)
	}

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	callers := []struct {
		name    string
		headers http.Header
	}{
		{name: "viewer", headers: http.Header{"X-Subject": {"view1"}, "X-Role": {"viewer"}}},
		{name: "operator", headers: http.Header{"X-Subject": {"op1"}, "X-Role": {"operator"}}},
		{name: "session-scoped-admin", headers: http.Header{
			"X-Subject": {"scoped1"}, "X-Role": {"admin"}, "X-Admin-Session-Scope": {"fo-view"},
		}},
	}
	for _, caller := range callers {
		t.Run(caller.name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			bc := dialBrowserWithHeaders(t, ctx, "ws"+strings.TrimPrefix(httpSrv.URL, "http")+"/ws/browser/fo-view/term", caller.headers)
			defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
			bc.waitFrame(t, "hello", 5*time.Second)

			worker.mu.Lock()
			before := len(worker.sent)
			worker.mu.Unlock()
			bc.send(t, ctx, map[string]any{"type": "fanout_send", "group_id": "viewer-group", "data": "id\n"})
			errFrame := bc.waitFrame(t, "error", 5*time.Second)
			if !strings.Contains(errFrame["message"].(string), "admin role required") {
				t.Fatalf("error frame = %#v", errFrame)
			}
			worker.mu.Lock()
			after := len(worker.sent)
			worker.mu.Unlock()
			if after != before {
				t.Fatalf("non-admin fanout wrote %d worker frames", after-before)
			}
		})
	}
}
