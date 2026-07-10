//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

// TestRunInspectNilContext covers the nil-context guard: runInspect defaults to
// context.Background() before registration, which fails against a dead server.
func TestRunInspectNilContext(t *testing.T) {
	dead := freePort(t)
	//nolint:staticcheck // deliberately passing a nil context to exercise the guard
	err := runInspect(nil, inspectOptions{Server: fmt.Sprintf("http://127.0.0.1:%d", dead), Port: 1},
		&bytes.Buffer{}, &bytes.Buffer{})
	if err == nil {
		t.Fatal("registration against a dead server should error")
	}
}

// TestRunInspectMissingWSEndpoint covers the missing-ws_endpoint branch.
func TestRunInspectMissingWSEndpoint(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"share_url": "x"}) // no ws_endpoint
	}))
	defer srv.Close()
	err := runInspect(context.Background(), inspectOptions{Server: srv.URL, Port: 1}, &bytes.Buffer{}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "ws_endpoint") {
		t.Fatalf("expected ws_endpoint error, got %v", err)
	}
}

// TestRunInspectConnectErrorAndInterceptBanner covers three branches at once: the
// no-tunnel-id "done" message, the intercept-ON banner, and the client.Connect
// failure (the advertised ws_endpoint points at a refused port).
func TestRunInspectConnectErrorAndInterceptBanner(t *testing.T) {
	dead := freePort(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		// No tunnel_id → the "done" (no id) banner branch.
		_ = json.NewEncoder(w).Encode(map[string]any{
			"ws_endpoint": fmt.Sprintf("ws://127.0.0.1:%d/tunnel", dead),
		})
	}))
	defer srv.Close()

	var out bytes.Buffer
	err := runInspect(context.Background(), inspectOptions{
		Server: srv.URL, Port: 1, Intercept: true, InterceptTimeout: 5, InterceptTimeoutAction: "forward",
	}, &out, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "connect") {
		t.Fatalf("expected tunnel connect error, got %v", err)
	}
	if !strings.Contains(out.String(), "Intercept: ON") {
		t.Errorf("intercept banner not printed: %q", out.String())
	}
	if !strings.Contains(out.String(), "done") {
		t.Errorf("done banner not printed: %q", out.String())
	}
}

// TestRunInspectLocalPortInUse covers the net.Listen failure branch after a
// successful tunnel connect.
func TestRunInspectLocalPortInUse(t *testing.T) {
	busy, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer busy.Close() //nolint:errcheck // test cleanup
	port := busy.Addr().(*net.TCPAddr).Port

	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		for {
			if _, _, rerr := c.Read(ctx); rerr != nil {
				return
			}
		}
	})
	err = runInspect(context.Background(), inspectOptions{Server: f.srv.URL, Port: 9, ListenPort: port},
		&bytes.Buffer{}, &bytes.Buffer{})
	if err == nil {
		t.Fatal("binding an in-use listen port should fail")
	}
}

// TestInspectServeError covers serve's non-ErrServerClosed error branch: a
// pre-closed listener makes the HTTP server's Serve fail immediately.
func TestInspectServeError(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	_ = ln.Close()
	sess := &inspectSession{
		client: tunnelclient.NewClient("ws://127.0.0.1:1/tunnel", ""),
		gate:   tunnelclient.NewInterceptGate(30, "forward"),
		errw:   io.Discard,
	}
	if err := sess.serve(context.Background(), ln); err == nil {
		t.Fatal("serving on a closed listener should error")
	}
}
