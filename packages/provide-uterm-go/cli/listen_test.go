//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/colors"
)

func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("free port: %v", err)
	}
	defer ln.Close() //nolint:errcheck
	return ln.Addr().(*net.TCPAddr).Port
}

func TestValidateListen(t *testing.T) {
	if _, err := validateListen(listenOptions{}); err == nil || !strings.Contains(err.Error(), "at least one") {
		t.Errorf("both ports zero should error, got %v", err)
	}
	_, err := validateListen(listenOptions{TelnetPort: 1, RequireAuthorizedKeys: true})
	if err == nil || !strings.Contains(err.Error(), "requires --authorized-keys") {
		t.Errorf("require without keys should error, got %v", err)
	}
	if _, err := validateListen(listenOptions{TelnetPort: 1, ColorMode: "neon"}); err == nil {
		t.Error("invalid color-mode should error")
	}
	mode, err := validateListen(listenOptions{TelnetPort: 1, ColorMode: "256"})
	if err != nil || mode != colors.Mode256 {
		t.Errorf("valid opts: mode=%q err=%v", mode, err)
	}
}

func TestBuildListenServersBoth(t *testing.T) {
	opts := listenOptions{
		WSURL: "ws://x/ws", Bind: "127.0.0.1",
		TelnetPort: freePort(t), SSHPort: freePort(t), ColorMode: "passthrough", IacNegotiate: false,
	}
	servers, err := buildListenServers(opts, colors.ModePassthrough)
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	if len(servers) != 2 {
		t.Fatalf("servers = %d, want 2", len(servers))
	}
	if !strings.Contains(servers[0].banner, "telnet://") || !strings.Contains(servers[1].banner, "ssh://") {
		t.Errorf("banners = %q / %q", servers[0].banner, servers[1].banner)
	}
	closeAll(servers)
}

func TestBuildListenServersAuthorizedKeysBanner(t *testing.T) {
	opts := listenOptions{
		WSURL: "ws://x/ws", Bind: "127.0.0.1", SSHPort: freePort(t),
		AuthorizedKeys: "/tmp/ak", RequireAuthorizedKeys: true,
	}
	servers, err := buildListenServers(opts, colors.ModePassthrough)
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	if !strings.Contains(servers[0].banner, "[pubkey: /tmp/ak (required)]") {
		t.Errorf("banner = %q", servers[0].banner)
	}
	closeAll(servers)
}

func TestBuildListenServersSecurityGate(t *testing.T) {
	// Non-loopback telnet without allow → fail-closed.
	_, err := buildListenServers(listenOptions{WSURL: "ws://x/ws", Bind: "0.0.0.0", TelnetPort: freePort(t)}, colors.ModePassthrough)
	if err == nil || !strings.Contains(err.Error(), "non-loopback") {
		t.Fatalf("telnet gate: %v", err)
	}
	// Non-loopback SSH without allow/require → fail-closed.
	_, err = buildListenServers(listenOptions{WSURL: "ws://x/ws", Bind: "0.0.0.0", SSHPort: freePort(t)}, colors.ModePassthrough)
	if err == nil || !strings.Contains(err.Error(), "non-loopback") {
		t.Fatalf("ssh gate: %v", err)
	}
}

func TestRunListenValidationErrors(t *testing.T) {
	if err := runListen(context.Background(), listenOptions{ColorMode: "passthrough"}, &bytes.Buffer{}); err == nil {
		t.Error("both ports zero should error")
	}
}

func TestServeListenCancel(t *testing.T) {
	opts := listenOptions{WSURL: "ws://x/ws", Bind: "127.0.0.1", TelnetPort: freePort(t), ColorMode: "passthrough"}
	servers, err := buildListenServers(opts, colors.ModePassthrough)
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	var out bytes.Buffer
	done := make(chan error, 1)
	go func() { done <- serveListen(ctx, servers, &out) }()
	time.Sleep(50 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Errorf("serveListen returned %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("serveListen did not return after cancel")
	}
	if !strings.Contains(out.String(), "telnet://") {
		t.Errorf("banner not printed: %q", out.String())
	}
}

func TestListenHelpUnchanged(t *testing.T) {
	var out, errw bytes.Buffer
	Execute([]string{"listen", "--help"}, &out, &errw)
	h := out.String()
	for _, want := range []string{"--port", "--ssh-port", "--server-key", "--color-mode",
		"--no-iac-negotiate", "--authorized-keys", "--require-authorized-keys",
		"--allow-unauthenticated-ssh", "--allow-unauthenticated-telnet"} {
		if !strings.Contains(h, want) {
			t.Errorf("help missing %q", want)
		}
	}
}
