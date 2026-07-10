//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"context"
	"net"
	"os"
	"strings"
	"testing"
	"time"
)

func TestConfigHelpers(t *testing.T) {
	m := map[string]string{"A": "1"}
	setDefault(m, "A", "2")
	setDefault(m, "B", "3")
	if m["A"] != "1" || m["B"] != "3" {
		t.Fatalf("setDefault: %+v", m)
	}
	if _, _, ok := splitEnv("noequals"); ok {
		t.Fatal("splitEnv should fail without '='")
	}
	if k, v, ok := splitEnv("K=V=x"); !ok || k != "K" || v != "V=x" {
		t.Fatalf("splitEnv: %q %q %v", k, v, ok)
	}
	if got := coerceStringList([]string{"a", "b"}); len(got) != 2 || got[0] != "a" {
		t.Fatalf("coerceStringList []string: %+v", got)
	}
	if got := coerceStringList("bogus"); got != nil {
		t.Fatalf("coerceStringList non-list should be nil: %+v", got)
	}
	if got := coerceStringList(nil); got != nil {
		t.Fatalf("coerceStringList nil: %+v", got)
	}
	if got := coerceIntOr(int64(7), 0); got != 7 {
		t.Fatalf("coerceIntOr int64: %d", got)
	}
	if got := coerceIntOr("x", 9); got != 9 {
		t.Fatalf("coerceIntOr default: %d", got)
	}
	if _, err := coerceEnv([]int{1}); err == nil {
		t.Fatal("coerceEnv non-map should error")
	}
	if _, err := coerceEnv(map[string]any{"K": 5}); err == nil {
		t.Fatal("coerceEnv non-string value should error")
	}
	env, err := coerceEnv(map[string]string{"K": "v"})
	if err != nil || env["K"] != "v" {
		t.Fatalf("coerceEnv map[string]string: %+v %v", env, err)
	}
	if s, ok := optString(map[string]any{"k": 5}, "k"); ok || s != "" {
		t.Fatalf("optString non-string: %q %v", s, ok)
	}
	if !containsInt([]int{1, 2, 3}, 2) || containsInt([]int{1}, 9) {
		t.Fatal("containsInt")
	}
	if p, err := coerceIntPtr(map[string]any{"n": int64(5)}, "n"); err != nil || p == nil || *p != 5 {
		t.Fatalf("coerceIntPtr int64: %v %v", p, err)
	}
	_ = isDarwin() // exercised on both OSes
}

func TestCapturePathAccessor(t *testing.T) {
	s, _ := NewCaptureSocket("/tmp/x.sock")
	if s.Path() != "/tmp/x.sock" {
		t.Fatalf("Path() = %q", s.Path())
	}
}

func TestBuildEnv(t *testing.T) {
	// Unset the login vars so the resolved-default (setdefault) branch actually
	// sets them — otherwise the inherited process env would already hold them.
	for _, k := range []string{"HOME", "SHELL", "USER", "LOGNAME"} {
		if old, ok := os.LookupEnv(k); ok {
			_ = os.Unsetenv(k)
			t.Cleanup(func() { _ = os.Setenv(k, old) })
		}
	}
	c := makeConn(t, "/bin/sh", nil, map[string]any{"env": map[string]any{"EXTRA": "e"}})
	resolved := &ResolvedUser{UID: 1000, GID: 1000, Home: "/home/x", Shell: "/bin/zsh", Name: "x"}
	env := c.buildEnv(map[string]string{"PAMVAR": "p"}, resolved, "/run/cap.sock")
	got := map[string]string{}
	for _, kv := range env {
		if k, v, ok := splitEnv(kv); ok {
			got[k] = v
		}
	}
	if got["HOME"] != "/home/x" || got["SHELL"] != "/bin/zsh" || got["USER"] != "x" || got["LOGNAME"] != "x" {
		t.Fatalf("resolved defaults missing: %+v", got)
	}
	if got["PAMVAR"] != "p" || got["EXTRA"] != "e" || got["UTERM_CAPTURE_SOCKET"] != "/run/cap.sock" {
		t.Fatalf("env vars missing: %+v", got)
	}
}

func TestSupplementaryGroupsCurrentUser(t *testing.T) {
	_, uid, gid := currentUser(t)
	groups := supplementaryGroups(&ResolvedUser{UID: uid, GID: gid, Name: "x"})
	found := false
	for _, g := range groups {
		if int(g) == gid {
			found = true
		}
	}
	if !found {
		t.Fatalf("primary gid %d not in supplementary groups %+v", gid, groups)
	}
}

func TestConnectorPollReturnsSnapshotWhenDirty(t *testing.T) {
	c := makeConn(t, "/bin/cat", nil, nil)
	if err := c.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Stop(context.Background()) }()
	c.HandleInput(context.Background(), "poke\n")
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if frames := c.PollMessages(); frames != nil {
			if frames[0]["type"] != "snapshot" {
				t.Fatalf("expected snapshot, got %+v", frames)
			}
			return
		}
		time.Sleep(15 * time.Millisecond)
	}
	t.Fatal("poll never returned a snapshot")
}

func TestConnectorReapKillPath(t *testing.T) {
	// Child ignores SIGHUP and keeps its stdin open (own fd), so closing the PTY
	// master does not make it exit — forcing the grace→SIGKILL escalation.
	c := makeConn(t, "/bin/sh", []string{"-c", "trap '' HUP; exec sleep 30 </dev/null"}, nil)
	if err := c.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	orig := stopGraceWindow
	stopGraceWindow = 60 * time.Millisecond
	defer func() { stopGraceWindow = orig }()

	done := make(chan struct{})
	go func() {
		_ = c.Stop(context.Background())
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Stop did not escalate to SIGKILL in time")
	}
	if c.IsConnected() {
		t.Fatal("should be disconnected after stop")
	}
}

func TestListenerOversizedLineDropped(t *testing.T) {
	path := shortSocketPath(t)
	l, _ := NewPamNotifyListener(path, nil)
	got := make(chan PamEvent, 4)
	_ = l.Start(context.Background(), func(_ context.Context, ev PamEvent) { got <- ev })
	defer func() { _ = l.Stop(context.Background()) }()

	conn, err := net.Dial("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	// One oversized line (no newline for >maxLine), then a newline to end it,
	// then a valid event line — the valid one must still be delivered.
	_, _ = conn.Write([]byte(strings.Repeat("A", notifyMaxLine*2) + "\n"))
	_, _ = conn.Write([]byte(`{"event":"open","username":"z","pid":7}` + "\n"))
	_ = conn.Close()

	select {
	case ev := <-got:
		if ev.Username != "z" {
			t.Fatalf("unexpected event %+v", ev)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("valid event after an oversized line was not delivered")
	}
}
