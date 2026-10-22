//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"context"
	"math"
	"os"
	"sync"
	"testing"
)

const (
	notAModeMessage    = "capture socket mode must be an integer: 0o600 or 0o660"
	wrongModeMessage   = "capture socket mode must be 0o600 or 0o660, got: "
	ownerAndGroupPerms = 0o660
)

// The two modes are owner-only and owner-and-group, and those are the two a
// mode check answers with as they are, from any integer type a config map
// holds: an int from the server, an int64 from TOML, a whole float64 from JSON.
func TestCheckSocketModeAcceptsTheTwoModes(t *testing.T) {
	if OwnerOnlySocketMode != 0o600 || OwnerAndGroupSocketMode != 0o660 {
		t.Fatalf("modes = %o, %o", OwnerOnlySocketMode, OwnerAndGroupSocketMode)
	}
	for _, tc := range []struct {
		in   any
		want int
	}{
		{0o600, 0o600}, {0o660, 0o660}, {int64(0o660), 0o660}, {float64(0o660), 0o660},
	} {
		got, err := CheckSocketMode(tc.in)
		if err != nil || got != tc.want {
			t.Fatalf("CheckSocketMode(%v) = %o, %v; want %o", tc.in, got, err, tc.want)
		}
	}
}

// Any other mode is refused, quoted in octal as Python's oct() writes it.
func TestCheckSocketModeRefusesAnyOtherMode(t *testing.T) {
	for _, tc := range []struct {
		in    any
		shown string
	}{
		{0o666, "0o666"}, {0o640, "0o640"}, {0o700, "0o700"}, {0o770, "0o770"}, {0o2660, "0o2660"},
		{0, "0o0"}, {-0o600, "-0o600"}, {int64(0o644), "0o644"}, {float64(0o644), "0o644"},
	} {
		_, err := CheckSocketMode(tc.in)
		if err == nil || err.Error() != wrongModeMessage+tc.shown {
			t.Fatalf("CheckSocketMode(%v) err = %v, want %q", tc.in, err, wrongModeMessage+tc.shown)
		}
	}
}

// A value that is not an integer is refused rather than read as one.
func TestCheckSocketModeRefusesWhatIsNotAnInteger(t *testing.T) {
	for _, in := range []any{
		"0o660", "432", 432.5, true, nil, []any{432},
		// A float no int holds is not a whole number of anything: int() of
		// one is implementation-defined, and would be quoted as nonsense.
		math.Inf(1), math.Inf(-1), math.NaN(), 1e300, -1e300, 0x1p63,
	} {
		if _, err := CheckSocketMode(in); err == nil || err.Error() != notAModeMessage {
			t.Fatalf("CheckSocketMode(%#v) err = %v, want %q", in, err, notAModeMessage)
		}
	}
}

// boundPerms starts s under the most permissive umask, so any bit the bind did
// not set itself shows, and answers the socket's permissions.
func boundPerms(t *testing.T, s *CaptureSocket) os.FileMode {
	t.Helper()
	prev := umaskSet(0)
	err := s.Start()
	umaskSet(prev)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Stop() })
	info, err := os.Stat(s.Path())
	if err != nil {
		t.Fatal(err)
	}
	return info.Mode().Perm()
}

func TestCaptureSocketIsOwnerOnlyByDefault(t *testing.T) {
	s, err := NewCaptureSocket(shortSocketPath(t))
	if err != nil {
		t.Fatal(err)
	}
	if perm := boundPerms(t, s); perm != 0o600 {
		t.Fatalf("perms = %o, want 600", perm)
	}
}

func TestCaptureSocketAskedForOwnerAndGroupIsBoundSo(t *testing.T) {
	s, err := NewCaptureSocketWithMode(shortSocketPath(t), ownerAndGroupPerms)
	if err != nil {
		t.Fatal(err)
	}
	if perm := boundPerms(t, s); perm != ownerAndGroupPerms {
		t.Fatalf("perms = %o, want 660", perm)
	}
}

// The umask the bind runs under leaves exactly the mode's bits.
func TestCaptureSocketBindUmask(t *testing.T) {
	if captureBindUmask(0o600) != 0o177 || captureBindUmask(0o660) != 0o117 {
		t.Fatalf("umasks = %o, %o", captureBindUmask(0o600), captureBindUmask(0o660))
	}
}

// A mode the socket would not be bound with is refused before anything is.
func TestCaptureSocketRefusesAModeBeforeBinding(t *testing.T) {
	path := shortSocketPath(t)
	if _, err := NewCaptureSocketWithMode(path, 0o666); err == nil || err.Error() != wrongModeMessage+"0o666" {
		t.Fatalf("err = %v", err)
	}
	if _, err := os.Lstat(path); !os.IsNotExist(err) {
		t.Fatalf("something is at %s: %v", path, err)
	}
}

// A capture connector binds its socket with the mode in its config, owner-only
// when there is none, and refuses one it would not bind with when it is built.
func TestCaptureConnectorBindsWithItsMode(t *testing.T) {
	for _, tc := range []struct {
		config map[string]any
		want   os.FileMode
	}{
		{map[string]any{}, 0o600},
		{map[string]any{"socket_mode": 0o600}, 0o600},
		{map[string]any{"socket_mode": 0o660}, 0o660},
	} {
		path := shortSocketPath(t)
		tc.config["socket_path"] = path
		c, err := NewCaptureConnector("s", "n", tc.config)
		if err != nil {
			t.Fatal(err)
		}
		prev := umaskSet(0)
		err = c.Start(context.Background())
		umaskSet(prev)
		if err != nil {
			t.Fatal(err)
		}
		info, err := os.Stat(path)
		_ = c.Stop(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if perm := info.Mode().Perm(); perm != tc.want {
			t.Fatalf("config %v: perms = %o, want %o", tc.config, perm, tc.want)
		}
	}
}

func TestCaptureConnectorRefusesAModeItWouldNotBind(t *testing.T) {
	for _, tc := range []struct {
		mode any
		want string
	}{
		{0o644, wrongModeMessage + "0o644"},
		{"0o660", notAModeMessage},
	} {
		_, err := NewCaptureConnector("s", "n", map[string]any{"socket_path": "/tmp/x.sock", "socket_mode": tc.mode})
		if err == nil || err.Error() != tc.want {
			t.Fatalf("socket_mode %v: err = %v, want %q", tc.mode, err, tc.want)
		}
	}
}

// The umask is process-wide. Two Starts setting and restoring it at once
// interleave: the second reads the first's bind umask as the one to restore
// and leaves it behind for every file the server creates afterwards (at 0o660,
// 0o117: wider than a service's 0o077 in the group bits). Concurrent Starts, of
// capture sockets and the notify socket together, leave it as it was.
func TestConcurrentStartsLeaveTheProcessUmaskAsItWas(t *testing.T) {
	prev := umaskSet(0o077)
	defer umaskSet(prev)
	for round := 0; round < 20; round++ {
		var wg sync.WaitGroup
		sockets := make([]*CaptureSocket, 16)
		for i := range sockets {
			s, err := NewCaptureSocketWithMode(shortSocketPath(t), OwnerAndGroupSocketMode)
			if err != nil {
				t.Fatal(err)
			}
			sockets[i] = s
		}
		notify, err := NewPamNotifyListener(shortSocketPath(t), nil)
		if err != nil {
			t.Fatal(err)
		}
		for _, s := range sockets {
			wg.Add(1)
			go func() { defer wg.Done(); _ = s.Start() }()
		}
		wg.Add(1)
		go func() { defer wg.Done(); _ = notify.Start(context.Background(), func(context.Context, PamEvent) {}) }()
		wg.Wait()
		after := umaskSet(0o077)
		for _, s := range sockets {
			_ = s.Stop()
		}
		_ = notify.Stop(context.Background())
		if after != 0o077 {
			t.Fatalf("round %d: umask left at %o, want 077", round, after)
		}
	}
}

// The bind itself runs under the umask that leaves exactly the mode's bits, so
// the socket never exists wider than its mode, even before the chmod after it.
func TestTheBindRunsUnderTheModesUmask(t *testing.T) {
	for _, mode := range []int{OwnerOnlySocketMode, OwnerAndGroupSocketMode} {
		var set []int
		real := umaskSet
		umaskSet = func(mask int) int { set = append(set, mask); return real(mask) }
		s, err := NewCaptureSocketWithMode(shortSocketPath(t), mode)
		if err != nil {
			umaskSet = real
			t.Fatal(err)
		}
		prev := real(0o022)
		err = s.Start()
		umaskSet = real
		real(prev)
		if err != nil {
			t.Fatal(err)
		}
		_ = s.Stop()
		if len(set) != 2 || set[0] != captureBindUmask(mode) || set[1] != 0o022 {
			t.Fatalf("mode %o: umasks set %o, want [%o 22]", mode, set, captureBindUmask(mode))
		}
	}
}
