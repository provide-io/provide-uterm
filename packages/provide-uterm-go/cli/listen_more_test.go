//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"errors"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/colors"
)

// TestRunListenNilContext covers the nil-context guard: runListen must default to
// context.Background() before validating flags (which then fails fast here).
func TestRunListenNilContext(t *testing.T) {
	//nolint:staticcheck // deliberately passing a nil context to exercise the guard
	if err := runListen(nil, listenOptions{ColorMode: "passthrough"}, &bytes.Buffer{}); err == nil {
		t.Fatal("both ports zero should error even with a nil context")
	}
}

// TestRunListenBuildError covers the buildListenServers failure branch: a
// non-loopback bind without an explicit allow flag fails closed before serving.
func TestRunListenBuildError(t *testing.T) {
	opts := listenOptions{WSURL: "ws://x/ws", Bind: "0.0.0.0", TelnetPort: freePort(t), ColorMode: "passthrough"}
	err := runListen(context.Background(), opts, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "non-loopback") {
		t.Fatalf("expected fail-closed build error, got %v", err)
	}
}

// TestRunListenServesThenCancel covers the happy path through runListen: it binds
// a real loopback telnet listener, serves it, and returns nil when the context is
// cancelled.
func TestRunListenServesThenCancel(t *testing.T) {
	opts := listenOptions{WSURL: "ws://x/ws", Bind: "127.0.0.1", TelnetPort: freePort(t), ColorMode: "passthrough"}
	ctx, cancel := context.WithCancel(context.Background())
	var out bytes.Buffer
	done := make(chan error, 1)
	go func() { done <- runListen(ctx, opts, &out) }()
	time.Sleep(50 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("runListen returned %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("runListen did not return after cancel")
	}
	if !strings.Contains(out.String(), "telnet://") {
		t.Errorf("banner not printed: %q", out.String())
	}
}

// TestServeListenServeError covers the errCh branch: when a serve function
// returns an error, serveListen tears the listeners down and surfaces it.
func TestServeListenServeError(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	boom := errors.New("serve failed")
	servers := []listenServer{{
		ln:     ln,
		serve:  func(context.Context, net.Listener) error { return boom },
		banner: "test-banner",
	}}
	got := serveListen(context.Background(), servers, &bytes.Buffer{})
	if !errors.Is(got, boom) {
		t.Fatalf("serveListen error = %v, want %v", got, boom)
	}
}

// TestBuildListenServersTelnetBindError covers the telnet-listener bind failure
// path: reusing an already-bound loopback port fails and closes any partials.
func TestBuildListenServersTelnetBindError(t *testing.T) {
	busy, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer busy.Close() //nolint:errcheck // test cleanup
	port := busy.Addr().(*net.TCPAddr).Port
	opts := listenOptions{WSURL: "ws://x/ws", Bind: "127.0.0.1", TelnetPort: port, ColorMode: "passthrough"}
	if _, err := buildListenServers(opts, colors.ModePassthrough); err == nil {
		t.Fatal("expected bind error on an in-use port")
	}
}
