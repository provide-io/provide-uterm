//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"bytes"
	"context"
	"encoding/binary"
	"fmt"
	"net"
	"os"
	"sync/atomic"
	"testing"
	"time"
)

var sockCounter atomic.Int64

// shortSocketPath returns a short absolute path under /tmp (unix socket paths
// are capped at ~104 bytes on macOS, so t.TempDir() can be too long).
func shortSocketPath(t *testing.T) string {
	t.Helper()
	p := fmt.Sprintf("/tmp/utpty-%d-%d.sock", os.Getpid(), sockCounter.Add(1))
	t.Cleanup(func() { _ = os.Remove(p) })
	return p
}

func makeFrame(channel int, data []byte) []byte {
	hdr := make([]byte, headerSize)
	hdr[0] = byte(channel)
	binary.BigEndian.PutUint32(hdr[1:], uint32(len(data)))
	return append(hdr, data...)
}

func sendFrames(t *testing.T, path string, frames ...[]byte) {
	t.Helper()
	conn, err := net.Dial("unix", path)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.Close() }()
	for _, f := range frames {
		if _, err := conn.Write(f); err != nil {
			t.Fatalf("write: %v", err)
		}
	}
}

func readFrameTimeout(t *testing.T, s *CaptureSocket) CaptureFrame {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	f, err := s.ReadFrame(ctx)
	if err != nil {
		t.Fatalf("read frame: %v", err)
	}
	return f
}

func TestCaptureChannelConstants(t *testing.T) {
	if ChannelStdout != 0x01 || ChannelStdin != 0x02 || ChannelConnect != 0x03 {
		t.Fatal("channel constants mismatch")
	}
}

func TestCaptureStartStop(t *testing.T) {
	path := shortSocketPath(t)
	s, err := NewCaptureSocket(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("socket file missing: %v", err)
	}
	if err := s.Stop(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("socket file should be removed, err=%v", err)
	}
}

func TestCaptureReceiveStdout(t *testing.T) {
	path := shortSocketPath(t)
	s, _ := NewCaptureSocket(path)
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Stop() }()
	sendFrames(t, path, makeFrame(ChannelStdout, []byte("hello world")))
	f := readFrameTimeout(t, s)
	if f.Channel != ChannelStdout || !bytes.Equal(f.Data, []byte("hello world")) {
		t.Fatalf("got %+v", f)
	}
}

func TestCaptureReceiveMultiple(t *testing.T) {
	path := shortSocketPath(t)
	s, _ := NewCaptureSocket(path)
	_ = s.Start()
	defer func() { _ = s.Stop() }()
	payloads := [][]byte{[]byte("frame1"), []byte("frame2"), []byte("frame3")}
	var frames [][]byte
	for _, p := range payloads {
		frames = append(frames, makeFrame(ChannelStdout, p))
	}
	sendFrames(t, path, frames...)
	for _, want := range payloads {
		got := readFrameTimeout(t, s)
		if !bytes.Equal(got.Data, want) {
			t.Fatalf("got %q want %q", got.Data, want)
		}
	}
}

func TestCaptureReceiveConnectFrame(t *testing.T) {
	path := shortSocketPath(t)
	s, _ := NewCaptureSocket(path)
	_ = s.Start()
	defer func() { _ = s.Stop() }()
	addr := []byte("192.168.1.1:8080")
	sendFrames(t, path, makeFrame(ChannelConnect, addr))
	f := readFrameTimeout(t, s)
	if f.Channel != ChannelConnect || !bytes.Equal(f.Data, addr) {
		t.Fatalf("got %+v", f)
	}
}

func TestCaptureStopWithoutStart(t *testing.T) {
	path := shortSocketPath(t)
	s, _ := NewCaptureSocket(path)
	if err := s.Stop(); err != nil {
		t.Fatalf("stop without start should be a no-op: %v", err)
	}
}

func TestCaptureStopSocketAlreadyRemoved(t *testing.T) {
	path := shortSocketPath(t)
	s, _ := NewCaptureSocket(path)
	_ = s.Start()
	_ = os.Remove(path)
	if err := s.Stop(); err != nil {
		t.Fatalf("stop with removed socket should not error: %v", err)
	}
}

func TestCaptureRejectsBadPath(t *testing.T) {
	if _, err := NewCaptureSocket("/tmp/ok\x00bad.sock"); err == nil {
		t.Fatal("expected null-byte rejection")
	}
	if _, err := NewCaptureSocket("relative.sock"); err == nil {
		t.Fatal("expected absolute-path rejection")
	}
}

func TestCaptureSocketPerms(t *testing.T) {
	path := shortSocketPath(t)
	s, _ := NewCaptureSocket(path)
	prevUmask := umaskSet(0) // most permissive — would surface any window
	_ = umaskSet(prevUmask)
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Stop() }()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("socket perms = %o, want 0600", perm)
	}
}

func TestCaptureDropOldestOnOverflow(t *testing.T) {
	path := shortSocketPath(t)
	s, err := newCaptureSocketWithQueue(path, 2)
	if err != nil {
		t.Fatal(err)
	}
	// Feed three frames through the framing path directly (deterministic, no
	// accept-loop scheduling races).
	r := bytes.NewReader(bytes.Join([][]byte{
		makeFrame(ChannelStdout, []byte("a")),
		makeFrame(ChannelStdout, []byte("b")),
		makeFrame(ChannelStdout, []byte("c")),
	}, nil))
	s.handleConn(r)
	if s.QueueLen() != 2 {
		t.Fatalf("queue len = %d, want 2", s.QueueLen())
	}
	first, _ := s.ReadNowait()
	second, _ := s.ReadNowait()
	if string(first.Data) != "b" || string(second.Data) != "c" {
		t.Fatalf("oldest not dropped: %q %q", first.Data, second.Data)
	}
}

func TestCaptureOversizedFrameRejected(t *testing.T) {
	path := shortSocketPath(t)
	s, _ := NewCaptureSocket(path)
	// A header announcing a body above the cap, followed by a body byte that
	// must NEVER be read. handleConn must short-circuit on the header alone.
	hdr := make([]byte, headerSize)
	hdr[0] = byte(ChannelStdout)
	binary.BigEndian.PutUint32(hdr[1:], maxFrameBytes+1)
	r := bytes.NewReader(append(hdr, 0xAA))
	s.handleConn(r)
	if s.QueueLen() != 0 {
		t.Fatalf("nothing should be enqueued, got %d", s.QueueLen())
	}
	// The body byte must remain unread.
	if rest, _ := r.ReadByte(); rest != 0xAA {
		t.Fatalf("body byte was consumed past the cap")
	}
}

func TestCaptureExactCapAccepted(t *testing.T) {
	path := shortSocketPath(t)
	s, _ := newCaptureSocketWithQueue(path, 8)
	// A frame whose length equals the cap boundary is accepted (> not >=). Use a
	// small synthetic cap by sending a normal frame — exercises the accept path.
	r := bytes.NewReader(makeFrame(ChannelStdout, []byte("abcd")))
	s.handleConn(r)
	if s.QueueLen() != 1 {
		t.Fatalf("want 1 enqueued, got %d", s.QueueLen())
	}
	f, _ := s.ReadNowait()
	if string(f.Data) != "abcd" {
		t.Fatalf("got %q", f.Data)
	}
}

func TestCaptureHandleConnPartialHeaderDropped(t *testing.T) {
	path := shortSocketPath(t)
	s, _ := NewCaptureSocket(path)
	// Truncated header → io.ReadFull errors → connection dropped, nothing queued.
	s.handleConn(bytes.NewReader([]byte{0x01, 0x00}))
	if s.QueueLen() != 0 {
		t.Fatalf("want empty queue, got %d", s.QueueLen())
	}
}

func TestCaptureReadFrameContextCancel(t *testing.T) {
	path := shortSocketPath(t)
	s, _ := NewCaptureSocket(path)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := s.ReadFrame(ctx); err == nil {
		t.Fatal("expected context error")
	}
}
