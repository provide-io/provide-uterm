//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"context"
	"net"
	"os"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestParseEvent(t *testing.T) {
	ctx := context.Background()
	cases := []struct {
		name string
		line string
		ok   bool
		want PamEvent
	}{
		{"open", `{"event":"open","username":"alice","tty":"/dev/pts/3","pid":123}`, true,
			PamEvent{Event: "open", Username: "alice", TTY: "/dev/pts/3", PID: 123, Mode: "notify"}},
		{"close", `{"event":"close","username":"bob","pid":9}`, true,
			PamEvent{Event: "close", Username: "bob", PID: 9, Mode: "notify"}},
		{"pid-string", `{"event":"open","username":"a","pid":"42"}`, true,
			PamEvent{Event: "open", Username: "a", PID: 42, Mode: "notify"}},
		{"capture-mode", `{"event":"open","username":"a","mode":"capture","capture_socket":"/run/c.sock"}`, true,
			PamEvent{Event: "open", Username: "a", Mode: "capture", CaptureSocket: "/run/c.sock"}},
		{"unknown-event", `{"event":"weird","username":"a"}`, false, PamEvent{}},
		{"missing-username", `{"event":"open"}`, false, PamEvent{}},
		{"bad-json", `not json`, false, PamEvent{}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ev, ok := parseEvent(ctx, []byte(tc.line))
			if ok != tc.ok {
				t.Fatalf("ok=%v want %v", ok, tc.ok)
			}
			if !ok {
				return
			}
			if ev.Event != tc.want.Event || ev.Username != tc.want.Username || ev.TTY != tc.want.TTY ||
				ev.PID != tc.want.PID || ev.Mode != tc.want.Mode || ev.CaptureSocket != tc.want.CaptureSocket {
				t.Fatalf("event mismatch: got %+v want %+v", ev, tc.want)
			}
		})
	}
}

func TestListenerBadSocketPath(t *testing.T) {
	if _, err := NewPamNotifyListener("relative.sock", nil); err == nil {
		t.Fatal("expected absolute-path rejection")
	}
}

func TestListenerRoundTrip(t *testing.T) {
	path := shortSocketPath(t)
	l, err := NewPamNotifyListener(path, nil)
	if err != nil {
		t.Fatal(err)
	}
	var (
		mu     sync.Mutex
		events []PamEvent
	)
	handler := func(_ context.Context, ev PamEvent) {
		mu.Lock()
		events = append(events, ev)
		mu.Unlock()
	}
	if err := l.Start(context.Background(), handler); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = l.Stop(context.Background()) }()

	// Owner-only perms.
	info, _ := os.Stat(path)
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("perms = %o, want 0600", info.Mode().Perm())
	}

	conn, err := net.Dial("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = conn.Write([]byte(`{"event":"open","username":"alice","pid":1}` + "\n"))
	_, _ = conn.Write([]byte(`{"event":"close","username":"alice","pid":1}` + "\n"))
	_ = conn.Close()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		n := len(events)
		mu.Unlock()
		if n >= 2 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(events) != 2 || events[0].Event != "open" || events[1].Event != "close" {
		t.Fatalf("events = %+v", events)
	}
}

func TestListenerAlreadyStarted(t *testing.T) {
	path := shortSocketPath(t)
	l, _ := NewPamNotifyListener(path, nil)
	if err := l.Start(context.Background(), func(context.Context, PamEvent) {}); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = l.Stop(context.Background()) }()
	if err := l.Start(context.Background(), func(context.Context, PamEvent) {}); err == nil {
		t.Fatal("expected already-started error")
	}
}

func TestListenerStopWithoutStart(t *testing.T) {
	path := shortSocketPath(t)
	l, _ := NewPamNotifyListener(path, nil)
	if err := l.Stop(context.Background()); err != nil {
		t.Fatalf("stop without start should be a no-op: %v", err)
	}
}

func TestListenerHandlerPanicRecovered(t *testing.T) {
	path := shortSocketPath(t)
	l, _ := NewPamNotifyListener(path, nil)
	var got int
	var mu sync.Mutex
	handler := func(_ context.Context, ev PamEvent) {
		mu.Lock()
		got++
		n := got
		mu.Unlock()
		if n == 1 {
			panic("boom") // first event panics; listener must survive
		}
	}
	_ = l.Start(context.Background(), handler)
	defer func() { _ = l.Stop(context.Background()) }()
	conn, _ := net.Dial("unix", path)
	_, _ = conn.Write([]byte(`{"event":"open","username":"a","pid":1}` + "\n"))
	_, _ = conn.Write([]byte(`{"event":"open","username":"b","pid":2}` + "\n"))
	_ = conn.Close()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		n := got
		mu.Unlock()
		if n >= 2 {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("listener did not survive a panicking handler")
}

func TestListenerPeerAllowlistReject(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("SO_PEERCRED peer-uid enforcement is Linux-only (macOS warns+allows)")
	}
	path := shortSocketPath(t)
	// Allowlist that cannot contain our uid.
	l, _ := NewPamNotifyListener(path, []int{-1})
	var called int
	var mu sync.Mutex
	handler := func(context.Context, PamEvent) {
		mu.Lock()
		called++
		mu.Unlock()
	}
	_ = l.Start(context.Background(), handler)
	defer func() { _ = l.Stop(context.Background()) }()
	conn, _ := net.Dial("unix", path)
	_, _ = conn.Write([]byte(`{"event":"open","username":"a","pid":1}` + "\n"))
	_ = conn.Close()
	time.Sleep(200 * time.Millisecond)
	mu.Lock()
	defer mu.Unlock()
	if called != 0 {
		t.Fatalf("rejected peer should not deliver events, got %d", called)
	}
}

func TestListenerStaleSocketUnlinked(t *testing.T) {
	path := shortSocketPath(t)
	// Pre-create a stale socket file that persists after close (Go normally
	// unlinks on close; SetUnlinkOnClose(false) leaves the socket inode behind).
	addr, err := net.ResolveUnixAddr("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	stale, err := net.ListenUnix("unix", addr)
	if err != nil {
		t.Fatal(err)
	}
	stale.SetUnlinkOnClose(false)
	_ = stale.Close()
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("stale socket should persist: %v", err)
	}
	l, _ := NewPamNotifyListener(path, nil)
	if err := l.Start(context.Background(), func(context.Context, PamEvent) {}); err != nil {
		t.Fatalf("start over stale socket: %v", err)
	}
	if !strings.HasPrefix(l.SocketPath(), "/tmp/") {
		t.Fatalf("unexpected socket path %q", l.SocketPath())
	}
	_ = l.Stop(context.Background())
}
