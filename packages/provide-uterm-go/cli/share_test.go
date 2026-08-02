//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

// fakeTunnelServer serves POST /api/tunnels and a /tunnel WebSocket. It records
// the registration request body and hands each accepted ws connection to onWS.
type fakeTunnelServer struct {
	srv       *httptest.Server
	mu        sync.Mutex
	regBodies []map[string]any
	regAuth   []string
}

func newFakeTunnelServer(t *testing.T, onWS func(ctx context.Context, c *websocket.Conn)) *fakeTunnelServer {
	t.Helper()
	f := &fakeTunnelServer{}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/tunnels", func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		f.mu.Lock()
		f.regBodies = append(f.regBodies, body)
		f.regAuth = append(f.regAuth, r.Header.Get("Authorization"))
		f.mu.Unlock()
		wsURL := "ws" + strings.TrimPrefix(f.srv.URL, "http") + "/tunnel"
		_ = json.NewEncoder(w).Encode(map[string]any{
			"tunnel_id":    "tid-1",
			"share_url":    "https://share.example/tid-1",
			"control_url":  "https://ctl.example/tid-1",
			"ws_endpoint":  wsURL,
			"worker_token": "wt-secret",
		})
	})
	mux.HandleFunc("/tunnel", func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer c.CloseNow() //nolint:errcheck // test cleanup
		onWS(r.Context(), c)
	})
	f.srv = httptest.NewServer(mux)
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeTunnelServer) firstReg() (map[string]any, string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.regBodies) == 0 {
		return nil, ""
	}
	return f.regBodies[0], f.regAuth[0]
}

func TestRunShareRegistersAndStreamsPTY(t *testing.T) {
	dataCh := make(chan []byte, 16)
	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		for {
			_, raw, err := c.Read(ctx)
			if err != nil {
				return
			}
			frame, derr := tunnelclient.DecodeFrame(raw)
			if derr != nil {
				continue
			}
			if frame.Channel == tunnelclient.ChannelData {
				dataCh <- append([]byte(nil), frame.Payload...)
				if frame.IsEOF() {
					return
				}
			}
		}
	})

	var out bytes.Buffer
	done := make(chan error, 1)
	go func() {
		done <- runShare(context.Background(), shareOptions{
			Server: f.srv.URL, Cmd: []string{"echo", "hi"}, DisplayName: "tester",
		}, &out)
	}()

	// The PTY child echoes "hi"; assert it arrives framed on ChannelData.
	var acc bytes.Buffer
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) && !bytes.Contains(acc.Bytes(), []byte("hi")) {
		select {
		case d := <-dataCh:
			acc.Write(d)
		case <-time.After(200 * time.Millisecond):
		}
	}
	if !bytes.Contains(acc.Bytes(), []byte("hi")) {
		t.Fatalf("never received framed PTY output, got %q", acc.Bytes())
	}

	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("runShare: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("runShare did not return after child exit")
	}

	// Registration assertions.
	body, authz := f.firstReg()
	if body["tunnel_type"] != "terminal" {
		t.Fatalf("tunnel_type = %v", body["tunnel_type"])
	}
	if body["display_name"] != "tester" {
		t.Fatalf("display_name = %v", body["display_name"])
	}
	if authz != "" {
		t.Fatalf("no token file → no Authorization, got %q", authz)
	}
	if !strings.Contains(out.String(), "https://share.example/tid-1") {
		t.Fatalf("share URL not printed: %q", out.String())
	}
}

func TestRunShareTokenFileSetsAuth(t *testing.T) {
	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		// Drain until close.
		for {
			if _, _, err := c.Read(ctx); err != nil {
				return
			}
		}
	})
	dir := t.TempDir()
	tokFile := dir + "/token"
	if err := os.WriteFile(tokFile, []byte("  filetoken\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	done := make(chan error, 1)
	go func() {
		done <- runShare(context.Background(), shareOptions{
			Server: f.srv.URL, Cmd: []string{"echo", "x"}, TokenFile: tokFile,
		}, &out)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("runShare hung")
	}
	if _, authz := f.firstReg(); authz != "Bearer filetoken" {
		t.Fatalf("token file should set Authorization, got %q", authz)
	}
}

func TestRunShareMissingWSEndpoint(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"share_url": "x"}) // no ws_endpoint
	}))
	defer srv.Close()
	err := runShare(context.Background(), shareOptions{Server: srv.URL, Cmd: []string{"echo"}}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "ws_endpoint") {
		t.Fatalf("expected ws_endpoint error, got %v", err)
	}
}

func TestRunShareRegistrationHTTPError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "nope", http.StatusForbidden)
	}))
	defer srv.Close()
	err := runShare(context.Background(), shareOptions{Server: srv.URL, Cmd: []string{"echo"}}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "HTTP 403") {
		t.Fatalf("expected HTTP 403 error, got %v", err)
	}
}

func TestOpenShareSourceAttachNonTTY(t *testing.T) {
	// Under `go test` stdin is not a terminal, so --attach must fail entering
	// raw mode. (The raw-mode success path needs a real controlling TTY and is
	// exercised by tunnelclient's TestTtyProxyRawModeOnRealPty.)
	if _, err := openShareSource(shareOptions{Attach: true}); err == nil {
		t.Skip("stdin happens to be a TTY in this environment; attach succeeded")
	}
}

func TestOpenShareSourceSpawn(t *testing.T) {
	src, err := openShareSource(shareOptions{Cmd: []string{"true"}})
	if err != nil {
		t.Fatalf("spawn source: %v", err)
	}
	_ = src.Close()
}

func TestShareDisplayName(t *testing.T) {
	if got := shareDisplayName("custom"); got != "custom" {
		t.Fatalf("override = %q", got)
	}
	if got := shareDisplayName(""); !strings.Contains(got, "@") {
		t.Fatalf("auto display name should contain @, got %q", got)
	}
}

func TestRunShareDefaultsNilContext(t *testing.T) {
	// A nil context must be substituted rather than panicking in the HTTP call.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "nope", http.StatusForbidden)
	}))
	defer srv.Close()
	//nolint:staticcheck // deliberately passing nil to cover the ctx guard
	err := runShare(nil, shareOptions{Server: srv.URL, Cmd: []string{"echo"}}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "HTTP 403") {
		t.Fatalf("expected HTTP 403 error, got %v", err)
	}
}

func TestRunShareFailsWhenSourceCannotOpen(t *testing.T) {
	// Registration succeeds, then --attach fails because `go test` stdin is not
	// a terminal: the share must abort with that error, not proceed to bridge a
	// source it never opened.
	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		<-ctx.Done()
		_ = c.CloseNow()
	})
	if _, err := openShareSource(shareOptions{Attach: true}); err == nil {
		t.Skip("stdin happens to be a TTY in this environment")
	}
	err := runShare(context.Background(), shareOptions{Server: f.srv.URL, Attach: true}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "raw mode") {
		t.Fatalf("expected a raw-mode error, got %v", err)
	}
}

func TestRunShareFailsWhenTunnelUnreachable(t *testing.T) {
	// Registration hands back a ws_endpoint nothing is listening on, so the
	// tunnel dial fails and the share reports it instead of hanging.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"tunnel_id": "tid-1", "ws_endpoint": "ws://127.0.0.1:1/tunnel", "worker_token": "wt",
		})
	}))
	defer srv.Close()
	err := runShare(context.Background(), shareOptions{Server: srv.URL, Cmd: []string{"true"}}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "cannot connect to tunnel") {
		t.Fatalf("expected a tunnel connect error, got %v", err)
	}
}
