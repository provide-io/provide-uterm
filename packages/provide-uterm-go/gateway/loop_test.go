//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"context"
	"testing"

	"github.com/coder/websocket"
)

// TestRunGatewaySessionRedirectFollow verifies a same-origin redirect is
// followed with an immediate reconnect to the rewritten URL.
func TestRunGatewaySessionRedirectFollow(t *testing.T) {
	st := &controlState{}
	var urls []string
	call := 0
	pump := func(_ context.Context, u string) (int, error) {
		urls = append(urls, u)
		call++
		if call == 1 {
			st.redirect = "/game?room=1"
			return -1, nil
		}
		return int(websocket.StatusNormalClosure), nil
	}
	runGatewaySession(context.Background(), sessionParams{
		wsURL:           "wss://h/ws/terminal",
		pump:            pump,
		clientConnected: func() bool { return true },
		st:              st,
		maxReconnects:   3,
		maxRedirects:    5,
	})
	if len(urls) != 2 || urls[1] != "wss://h/game?room=1" {
		t.Fatalf("urls = %v", urls)
	}
}

// TestRunGatewaySessionReconnect verifies a transient drop triggers a reconnect
// (no delay) and stops on a deliberate normal closure.
func TestRunGatewaySessionReconnect(t *testing.T) {
	st := &controlState{}
	call := 0
	reconnects := 0
	pump := func(_ context.Context, _ string) (int, error) {
		call++
		if call == 1 {
			return -1, nil // transient drop
		}
		return int(websocket.StatusNormalClosure), nil
	}
	runGatewaySession(context.Background(), sessionParams{
		wsURL:           "wss://h/ws",
		pump:            pump,
		clientConnected: func() bool { return true },
		showReconnect:   func() { reconnects++ },
		st:              st,
		maxReconnects:   3,
		reconnectDelay:  0,
		maxRedirects:    5,
	})
	if call != 2 || reconnects != 1 {
		t.Fatalf("call=%d reconnects=%d", call, reconnects)
	}
}

// TestRunGatewaySessionRejectedRedirect stops on a cross-origin redirect.
func TestRunGatewaySessionRejectedRedirect(t *testing.T) {
	st := &controlState{}
	call := 0
	pump := func(_ context.Context, _ string) (int, error) {
		call++
		st.redirect = "https://evil/x"
		return -1, nil
	}
	runGatewaySession(context.Background(), sessionParams{
		wsURL: "wss://h/ws", pump: pump, clientConnected: func() bool { return true },
		st: st, maxReconnects: 3, maxRedirects: 5,
	})
	if call != 1 {
		t.Fatalf("cross-origin redirect should stop after one pump, got %d", call)
	}
}

// TestRunGatewaySessionRedirectCap stops after too many redirects.
func TestRunGatewaySessionRedirectCap(t *testing.T) {
	st := &controlState{}
	call := 0
	pump := func(_ context.Context, _ string) (int, error) {
		call++
		st.redirect = "/loop"
		return -1, nil
	}
	runGatewaySession(context.Background(), sessionParams{
		wsURL: "wss://h/ws", pump: pump, clientConnected: func() bool { return true },
		st: st, maxReconnects: 100, maxRedirects: 3,
	})
	if call != 4 { // initial + 3 redirects, then cap exceeded
		t.Fatalf("call = %d, want 4", call)
	}
}

// TestRunGatewaySessionClientGone returns immediately when the client is gone.
func TestRunGatewaySessionClientGone(t *testing.T) {
	call := 0
	runGatewaySession(context.Background(), sessionParams{
		wsURL:           "wss://h/ws",
		pump:            func(context.Context, string) (int, error) { call++; return -1, nil },
		clientConnected: func() bool { return false },
		st:              &controlState{},
		maxReconnects:   3,
	})
	if call != 0 {
		t.Fatalf("pump should not run when client disconnected, got %d", call)
	}
}
