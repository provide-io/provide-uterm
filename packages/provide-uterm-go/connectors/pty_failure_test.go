//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// TestPTYResolveCommandShellDefault covers the $SHELL / /bin/sh fallback chain
// for an empty command.
func TestPTYResolveCommandShellDefault(t *testing.T) {
	t.Setenv("SHELL", "/usr/bin/fakesh")
	if got := NewPTYTransport(nil).resolveCommand(); len(got) != 1 || got[0] != "/usr/bin/fakesh" {
		t.Fatalf("with $SHELL: %v", got)
	}
	t.Setenv("SHELL", "")
	if got := NewPTYTransport(nil).resolveCommand(); len(got) != 1 || got[0] != "/bin/sh" {
		t.Fatalf("without $SHELL: %v", got)
	}
	if got := NewPTYTransport([]string{"cat", "-u"}).resolveCommand(); len(got) != 2 {
		t.Fatalf("explicit command: %v", got)
	}
}

// TestPTYConnectSpawnFailure drives a real spawn failure (a command that does
// not exist) rather than stubbing the pty package.
func TestPTYConnectSpawnFailure(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "definitely-not-a-binary")
	tr := NewPTYTransport([]string{missing})
	err := tr.Connect(context.Background(), "", 0, transports.ConnectOptions{})
	if err == nil {
		t.Fatal("Connect to a missing binary should fail")
	}
	if tr.IsConnected() {
		t.Fatal("a failed Connect must leave the transport disconnected")
	}
}

// TestPTYSendOnClosedMaster writes to a genuinely closed file handle, which is
// what a Send after the PTY master died looks like: the write fails, the
// transport tears itself down, and the caller sees the error.
func TestPTYSendOnClosedMaster(t *testing.T) {
	f, err := os.CreateTemp(t.TempDir(), "pty-master")
	if err != nil {
		t.Fatalf("temp file: %v", err)
	}
	if err := f.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	tr := NewPTYTransport([]string{"cat"})
	tr.mu.Lock()
	tr.master = f
	tr.mu.Unlock()

	if err := tr.Send(context.Background(), []byte("x")); err == nil {
		t.Fatal("Send on a closed master should fail")
	}
	if tr.IsConnected() {
		t.Fatal("a failed Send must disconnect the transport")
	}
}

// TestPTYReceiveTimeout covers the timer arm: a live PTY with nothing to say
// returns an empty slice, not an error.
func TestPTYReceiveTimeout(t *testing.T) {
	ctx := context.Background()
	tr := NewPTYTransport([]string{"cat"})
	if err := tr.Connect(ctx, "", 0, transports.ConnectOptions{}); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(ctx) }()

	got, err := tr.Receive(ctx, 4096, 20*time.Millisecond)
	if err != nil {
		t.Fatalf("Receive: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("idle Receive returned %q", got)
	}
}

// TestSSHConnectorDialsAndFails builds the SSH connector's session for real and
// dials a port with nothing listening, so the transport-construction closure is
// exercised end to end instead of merely constructed.
func TestSSHConnectorDialsAndFails(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	addr, ok := ln.Addr().(*net.TCPAddr)
	if !ok {
		t.Fatalf("unexpected addr type %T", ln.Addr())
	}
	port := addr.Port
	if err := ln.Close(); err != nil {
		t.Fatalf("close listener: %v", err)
	}

	c, err := newSSH("s", "n", map[string]any{
		"host": "127.0.0.1", "port": port, "insecure_no_host_check": true,
	})
	if err != nil {
		t.Fatalf("newSSH: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := c.Start(ctx); err == nil {
		_ = c.Stop(ctx)
		t.Fatal("dialing a closed port should fail")
	}
}
