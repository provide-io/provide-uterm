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
)

// DeckMux collaborative-presence integration tests.

// TestDeckPresenceSyncOnJoin verifies a joining browser receives a presence_sync.
func TestDeckPresenceSyncOnJoin(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()
	ts.reg.add("deck", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	bc := dialBrowser(t, ctx, wsBase+"/ws/browser/deck/term", "admin1", "admin")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.waitFrame(t, "hello", 5*time.Second)
	sync := bc.waitFrame(t, "presence_sync", 5*time.Second)
	users, ok := sync["users"].([]any)
	if !ok || len(users) != 1 {
		t.Fatalf("presence_sync users = %v", sync["users"])
	}
}

// TestDeckPresenceBroadcastSecondBrowser verifies the first browser observes a
// presence_sync (2 users) when a second browser joins.
func TestDeckPresenceBroadcastSecondBrowser(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()
	ts.reg.add("deck2", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	b1 := dialBrowser(t, ctx, wsBase+"/ws/browser/deck2/term", "admin1", "admin")
	defer func() { _ = b1.conn.Close(websocket.StatusNormalClosure, "") }()
	b1.waitFrame(t, "hello", 5*time.Second)
	b1.waitFrame(t, "presence_sync", 5*time.Second) // b1's own sync (1 user)

	b2 := dialBrowser(t, ctx, wsBase+"/ws/browser/deck2/term", "op1", "operator")
	defer func() { _ = b2.conn.Close(websocket.StatusNormalClosure, "") }()
	b2.waitFrame(t, "hello", 5*time.Second)

	// b1 receives a broadcast presence_sync now reflecting both users.
	b1.waitFrameWhere(t, "presence_sync", 5*time.Second, func(f map[string]any) bool {
		u, ok := f["users"].([]any)
		return ok && len(u) == 2
	})
}

// TestDeckControlRequestTransfer verifies a control_request yields a
// control_transfer granting control to the requester.
func TestDeckControlRequestTransfer(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()
	ts.reg.add("deck3", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	bc := dialBrowser(t, ctx, wsBase+"/ws/browser/deck3/term", "admin1", "admin")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.waitFrame(t, "hello", 5*time.Second)
	bc.waitFrame(t, "presence_sync", 5*time.Second)

	bc.send(t, ctx, map[string]any{"type": "control_request"})
	xfer := bc.waitFrame(t, "control_transfer", 5*time.Second)
	if xfer["to_user_id"] != "admin1" || xfer["reason"] != "handover" {
		t.Fatalf("control_transfer frame: %v", xfer)
	}
}
