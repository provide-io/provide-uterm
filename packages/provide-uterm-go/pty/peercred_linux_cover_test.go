//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

//go:build linux

package pty

import (
	"net"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"
)

// connectedUnixPair returns a connected server-side *net.UnixConn.
func connectedUnixPair(t *testing.T) *net.UnixConn {
	t.Helper()
	path := filepath.Join(t.TempDir(), "p.sock")
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	client, err := net.Dial("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	server, err := ln.Accept()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = server.Close() })
	return server.(*net.UnixConn)
}

// Every way of failing to read the peer's credentials reports "unavailable"
// rather than a uid: a listener must never authorise on a guessed identity.
func TestPeerEUIDUnavailable(t *testing.T) {
	if _, ok := peerEUID(&net.UnixConn{}); ok {
		t.Fatal("a conn with no socket has no peer credentials")
	}

	closed := connectedUnixPair(t)
	_ = closed.Close()
	if _, ok := peerEUID(closed); ok {
		t.Fatal("a closed conn has no peer credentials")
	}

	orig := getsockoptUcred
	getsockoptUcred = func(int, int, int) (*unix.Ucred, error) { return nil, unix.ENOTSOCK }
	t.Cleanup(func() { getsockoptUcred = orig })
	if _, ok := peerEUID(connectedUnixPair(t)); ok {
		t.Fatal("a failed SO_PEERCRED read must not yield a uid")
	}
}
