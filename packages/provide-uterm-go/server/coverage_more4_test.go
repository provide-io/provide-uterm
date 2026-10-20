//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// TestBrowserConnClose covers browserConn.Close (the hub.BrowserCloser surface,
// used by the behavioral-audit deny path) over a real socket.
func TestBrowserConnClose(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		bc := &browserConn{wsBase: wsBase{conn: conn}}
		_ = bc.Close(context.Background(), 1008, "policy")
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "") }()
	// The server closed the socket → the client's read fails.
	if _, _, rerr := conn.Read(ctx); rerr == nil {
		t.Fatal("expected close from server-side browserConn.Close")
	}
}

// TestBrowserConnCloseReportsAPeerThatIgnoresTheClose: a peer that never
// reads cannot answer the close handshake. Close stops waiting after the grace
// and says so, rather than reporting a clean close. The grace is shortened on
// this connection only, so the test does not wait the production two seconds.
func TestBrowserConnCloseReportsAPeerThatIgnoresTheClose(t *testing.T) {
	const grace = 50 * time.Millisecond
	closed := make(chan error, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		bc := &browserConn{wsBase: wsBase{conn: conn}, closeGrace: grace}
		started := time.Now()
		err = bc.Close(context.Background(), 4410, "viewer access revoked")
		if took := time.Since(started); took > grace+time.Second {
			err = fmt.Errorf("close waited %v, past the grace", took)
		}
		closed <- err
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.CloseNow() }()
	// Never read: the server's close frame goes unanswered.
	select {
	case err := <-closed:
		if !errors.Is(err, errBrowserCloseTimedOut) {
			t.Fatalf("Close = %v, want errBrowserCloseTimedOut", err)
		}
	case <-ctx.Done():
		t.Fatal("Close did not return")
	}
}

// TestSweepIdleDisconnectsIdleWorker covers sweepIdleSessions' candidate loop: a
// browserless worker whose last activity is old is disconnected.
func TestSweepIdleDisconnectsIdleWorker(t *testing.T) {
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.SessionIdleTimeoutS = 1
	})
	ts.setupWorker(t, "idle")
	// Force the worker's last activity far into the past (negative monotonic, so
	// the elapsed exceeds the idle timeout regardless of the test clock origin).
	if st := ts.hub.Registry.Get("idle"); st != nil {
		st.LastActivityAt = -10000
	} else {
		t.Fatal("worker state missing")
	}
	ctx := context.Background()
	// The worker is an idle candidate, so the sweep loop body executes.
	if len(ts.hub.GetIdleCandidates(ctx, 1)) == 0 {
		t.Fatal("expected an idle candidate")
	}
	ts.srv.sweepIdleSessions(ctx)
}
