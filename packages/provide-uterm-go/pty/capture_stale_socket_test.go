//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"bytes"
	"net"
	"os"
	"testing"
)

// leaveStaleSocket binds path and drops the listener without unlinking it, as
// a process killed mid-session leaves its socket.
func leaveStaleSocket(t *testing.T, path string) {
	t.Helper()
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	ln.(*net.UnixListener).SetUnlinkOnClose(false)
	_ = ln.Close()
	if info, err := os.Lstat(path); err != nil || info.Mode()&os.ModeSocket == 0 {
		t.Fatalf("no stale socket left at %s: %v", path, err)
	}
}

// A socket a crashed server left behind no longer fails the bind: a capture
// session re-created after the restart listens at the same path and receives.
func TestCaptureStartRebindsOverAStaleSocket(t *testing.T) {
	path := shortSocketPath(t)
	leaveStaleSocket(t, path)
	s, err := NewCaptureSocket(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Start(); err != nil {
		t.Fatalf("Start over a stale socket: %v", err)
	}
	defer func() { _ = s.Stop() }()
	sendFrames(t, path, makeFrame(ChannelStdout, []byte("back")))
	if f := readFrameTimeout(t, s); !bytes.Equal(f.Data, []byte("back")) {
		t.Fatalf("got %+v", f)
	}
}

// Only a socket is removed. A regular file at the path is somebody else's: it
// stays, and the bind fails on it.
func TestCaptureStartLeavesARegularFileAlone(t *testing.T) {
	path := shortSocketPath(t)
	if err := os.WriteFile(path, []byte("not a socket"), 0o600); err != nil {
		t.Fatal(err)
	}
	s, _ := NewCaptureSocket(path)
	if err := s.Start(); err == nil {
		_ = s.Stop()
		t.Fatal("Start bound over a regular file")
	}
	if data, err := os.ReadFile(path); err != nil || string(data) != "not a socket" {
		t.Fatalf("the file was touched: %q, %v", data, err)
	}
}

// A symlink at the path is not followed: neither it nor the socket it points
// at is removed.
func TestCaptureStartDoesNotFollowASymlink(t *testing.T) {
	path := shortSocketPath(t)
	target := shortSocketPath(t)
	leaveStaleSocket(t, target)
	if err := os.Symlink(target, path); err != nil {
		t.Fatal(err)
	}
	s, _ := NewCaptureSocket(path)
	if err := s.Start(); err == nil {
		_ = s.Stop()
		t.Fatal("Start bound over a symlink")
	}
	if _, err := os.Lstat(path); err != nil {
		t.Fatalf("the symlink went: %v", err)
	}
	if info, err := os.Lstat(target); err != nil || info.Mode()&os.ModeSocket == 0 {
		t.Fatalf("the socket it points at went: %v", err)
	}
}

// A socket another capture session still serves is left to it: the bind
// fails, and the first session keeps its path and its producers.
func TestCaptureStartLeavesALiveSocketToItsServer(t *testing.T) {
	path := shortSocketPath(t)
	first, err := NewCaptureSocket(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := first.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = first.Stop() }()
	second, _ := NewCaptureSocket(path)
	if err := second.Start(); err == nil {
		_ = second.Stop()
		t.Fatal("a second capture took a live socket")
	}
	sendFrames(t, path, makeFrame(ChannelStdout, []byte("still mine")))
	if f := readFrameTimeout(t, first); !bytes.Equal(f.Data, []byte("still mine")) {
		t.Fatalf("got %+v", f)
	}
}

// A socket that does not say it is stale (this user may not even connect to
// it) is left alone too.
func TestCaptureStartLeavesASocketThatWillNotSay(t *testing.T) {
	path := shortSocketPath(t)
	leaveStaleSocket(t, path)
	if err := os.Chmod(path, 0); err != nil {
		t.Fatal(err)
	}
	s, _ := NewCaptureSocket(path)
	if err := s.Start(); err == nil {
		_ = s.Stop()
		t.Fatal("bound over a socket that did not say it was stale")
	}
	if info, err := os.Lstat(path); err != nil || info.Mode()&os.ModeSocket == 0 {
		t.Fatalf("the socket went: %v", err)
	}
}
