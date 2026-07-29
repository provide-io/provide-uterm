//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"errors"
	"net"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
)

// fakeAddr reports an arbitrary address string.
type fakeAddr string

func (fakeAddr) Network() string      { return "tcp" }
func (a fakeAddr) String() string     { return string(a) }
func (fakeListener) Close() error     { return nil }
func (l fakeListener) Addr() net.Addr { return l.addr }
func (fakeListener) Accept() (net.Conn, error) {
	return nil, errors.New("fake listener does not accept")
}

// fakeListener is a listener with a caller-chosen address.
type fakeListener struct{ addr net.Addr }

func TestNewLiveServerRejectsBadInput(t *testing.T) {
	t.Setenv("UTERM_DEV_TOKEN_PATH", filepath.Join(t.TempDir(), "dev_token"))
	ctx := context.Background()

	if _, err := NewLiveServer(ctx, nil, LiveServerOptions{}); err == nil {
		t.Fatal("expected an error for a nil listener")
	}

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer func() { _ = ln.Close() }()
	_, err = NewLiveServer(ctx, ln, LiveServerOptions{
		ConfigPath: filepath.Join(t.TempDir(), "missing.toml"),
	})
	if err == nil {
		t.Fatal("expected a config load error")
	}
}

func TestListenerHostPortErrors(t *testing.T) {
	if _, _, err := listenerHostPort(fakeListener{addr: fakeAddr("no-colon")}); err == nil {
		t.Fatal("expected a split error for an address with no port")
	}
	if _, _, err := listenerHostPort(fakeListener{addr: fakeAddr("127.0.0.1:http")}); err == nil {
		t.Fatal("expected a parse error for a non-numeric port")
	}
	host, port, err := listenerHostPort(fakeListener{addr: fakeAddr("127.0.0.1:1234")})
	if err != nil || host != "127.0.0.1" || port != 1234 {
		t.Fatalf("listenerHostPort = %q %d %v", host, port, err)
	}
}

func TestNewLiveServerRejectsAnUnusableListenerAddress(t *testing.T) {
	t.Setenv("UTERM_DEV_TOKEN_PATH", filepath.Join(t.TempDir(), "dev_token"))
	_, err := NewLiveServer(context.Background(), fakeListener{addr: fakeAddr("no-colon")}, LiveServerOptions{})
	if err == nil {
		t.Fatal("expected the address split error to surface")
	}
}

func TestLiveServerReportsTheOSAssignedPort(t *testing.T) {
	t.Setenv("UTERM_DEV_TOKEN_PATH", filepath.Join(t.TempDir(), "dev_token"))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv, err := NewLiveServer(ctx, ln, LiveServerOptions{AuthMode: "dev_token"})
	if err != nil {
		t.Fatalf("NewLiveServer: %v", err)
	}
	defer func() { _ = srv.Close() }()

	if srv.BaseURL() != "http://"+ln.Addr().String() {
		t.Fatalf("BaseURL = %q, want the listener's address", srv.BaseURL())
	}
	if strings.HasSuffix(srv.BaseURL(), ":0") {
		t.Fatalf("BaseURL reports the bind sentinel: %q", srv.BaseURL())
	}
	if srv.Token() == "" {
		t.Fatal("dev_token mode must mint a token")
	}
	// SetupDevIDP rewrites the mode to jwt so the standard validator accepts
	// the token it minted.
	if srv.AuthMode() != "jwt" {
		t.Fatalf("effective auth mode = %q, want jwt", srv.AuthMode())
	}

	serveErr := make(chan error, 1)
	go func() { serveErr <- srv.Serve(ctx) }()

	_, port, err := listenerHostPort(ln)
	if err != nil {
		t.Fatalf("listener port: %v", err)
	}
	if got := getWithRetry(t, port, "/api/health"); got.status != http.StatusOK {
		t.Fatalf("health status = %d", got.status)
	}

	cancel()
	if err := <-serveErr; err != nil {
		t.Fatalf("Serve: %v", err)
	}
}

func TestLiveServerAuthModeOverrideIsOptional(t *testing.T) {
	t.Setenv("UTERM_DEV_TOKEN_PATH", filepath.Join(t.TempDir(), "dev_token"))
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer func() { _ = ln.Close() }()
	// No AuthMode: the config's own default (dev_token) applies.
	srv, err := NewLiveServer(context.Background(), ln, LiveServerOptions{AuthMode: "  "})
	if err != nil {
		t.Fatalf("NewLiveServer: %v", err)
	}
	defer func() { _ = srv.Close() }()
	if srv.Token() == "" {
		t.Fatal("the default config is dev_token and must mint a token")
	}
}
