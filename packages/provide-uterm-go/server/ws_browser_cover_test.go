//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"errors"
	"net/http"
	"os"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// testModeEnv turns on the open-admin test mode for the duration of a test.
func testModeEnv(t *testing.T) {
	t.Helper()
	t.Setenv("UTERM_TEST_MODE", "1")
	t.Cleanup(func() { _ = os.Unsetenv("UTERM_TEST_MODE") })
}

// TestBrowserWSTestModeRejectsOverQuota proves the open-admin test-mode path
// still enforces the per-principal connection quota: test mode relaxes
// authentication, not resource limits. Every test-mode browser shares the
// synthetic "test-admin" principal, so the second connection is refused.
func TestBrowserWSTestModeRejectsOverQuota(t *testing.T) {
	testModeEnv(t)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Hub = hub.NewTermHub(hub.TermHubConfig{
			Clock: deps.Clock, MaxConnectionsPerPrincipal: 1, OnMetric: deps.Metrics.Inc, Logger: deps.Logger,
		})
	})
	ts.hub = ts.srv.deps.Hub
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	first, _, err := websocket.Dial(ctx, base+"/ws/browser/quota-w/term", nil)
	if err != nil {
		t.Fatalf("first dial: %v", err)
	}
	defer func() { _ = first.Close(websocket.StatusNormalClosure, "") }()
	// Read the hello so the first registration is definitely complete before the
	// second connection contends for the quota.
	if _, _, err := first.Read(ctx); err != nil {
		t.Fatalf("first hello: %v", err)
	}

	second, _, err := websocket.Dial(ctx, base+"/ws/browser/quota-w/term", nil)
	if err != nil {
		t.Fatalf("second dial: %v", err)
	}
	defer func() { _ = second.Close(websocket.StatusNormalClosure, "") }()
	_, _, err = second.Read(ctx)
	if err == nil {
		t.Fatal("the over-quota browser was served instead of refused")
	}
	if got := websocket.CloseStatus(err); got != websocket.StatusPolicyViolation {
		t.Fatalf("close status = %v, want %v", got, websocket.StatusPolicyViolation)
	}
}

// TestBrowserWSTestModeAbortsOnSetupFailure proves the test-mode path unwinds
// the registration it already performed when post-registration setup fails,
// rather than leaving a half-registered browser behind.
func TestBrowserWSTestModeAbortsOnSetupFailure(t *testing.T) {
	testModeEnv(t)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.BrowserSetupHook = func() error { return errors.New("setup failed") }
	})
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	conn, _, err := websocket.Dial(ctx, base+"/ws/browser/setup-w/term", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "") }()
	_, _, err = conn.Read(ctx)
	if err == nil {
		t.Fatal("the browser was served despite a failed setup hook")
	}
	if got := websocket.CloseStatus(err); got != websocket.StatusInternalError {
		t.Fatalf("close status = %v, want %v", got, websocket.StatusInternalError)
	}
	// The aborted browser left no registration behind.
	waitUntil(t, 5*time.Second, func() bool {
		return ts.hub.BrowserCount(context.Background(), "setup-w") == 0
	})
}

// TestBrowserWSClosesOnMalformedControlFrame drives a real inline-framing
// protocol error (a control frame whose declared payload is not JSON). The
// receive loop must close the socket with 1003 rather than resynchronising on a
// stream it can no longer parse.
func TestBrowserWSClosesOnMalformedControlFrame(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bad-frame", "admin1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	conn, _, err := websocket.Dial(ctx, base+"/ws/browser/bad-frame/term",
		&websocket.DialOptions{HTTPHeader: http.Header{"X-Subject": {"admin1"}, "X-Role": {"admin"}}})
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "") }()
	conn.SetReadLimit(1 << 20)
	if _, _, err := conn.Read(ctx); err != nil { // hello
		t.Fatalf("hello: %v", err)
	}

	// DLE STX + a well-formed 8-hex length + ':' + a payload that is not JSON.
	if err := conn.Write(ctx, websocket.MessageText, []byte("\x10\x0200000003:abc")); err != nil {
		t.Fatalf("write malformed frame: %v", err)
	}
	for {
		_, _, err = conn.Read(ctx)
		if err != nil {
			break
		}
	}
	if got := websocket.CloseStatus(err); got != websocket.StatusUnsupportedData {
		t.Fatalf("close status = %v, want %v", got, websocket.StatusUnsupportedData)
	}
}
