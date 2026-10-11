//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"context"
	"errors"
	"io"
	"io/fs"
	"net"
	"os"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

var errSeam = errors.New("injected failure")

func failChmod(t *testing.T) {
	t.Helper()
	orig := chmodSocket
	chmodSocket = func(string, fs.FileMode) error { return errSeam }
	t.Cleanup(func() { chmodSocket = orig })
}

// occupyWithDirectory replaces the socket file at path with a non-empty
// directory, which no Remove can delete (whoever runs the test).
func occupyWithDirectory(t *testing.T, path string) {
	t.Helper()
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(path, "occupied"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(path) })
}

// dialAndExpectClose connects to path and expects the server to hang up.
func dialAndExpectClose(t *testing.T, path string) {
	t.Helper()
	c, err := net.Dial("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close() }()
	_ = c.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, err := c.Read(make([]byte, 1)); !errors.Is(err, io.EOF) {
		t.Fatalf("read = %v, want the server to close the connection", err)
	}
}

func TestCaptureStartChmodFailure(t *testing.T) {
	failChmod(t)
	s, err := NewCaptureSocket(shortSocketPath(t))
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Start(); !errors.Is(err, errSeam) {
		t.Fatalf("Start = %v, want the chmod failure", err)
	}
	if s.started {
		t.Fatal("a socket whose mode could not be tightened must not be started")
	}
}

func TestCaptureAcceptAfterCloseDropsConnection(t *testing.T) {
	path := shortSocketPath(t)
	s, err := NewCaptureSocket(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	s.mu.Lock()
	s.closed = true
	s.mu.Unlock()
	dialAndExpectClose(t, path)
	s.mu.Lock()
	s.closed = false
	n := len(s.conns)
	s.mu.Unlock()
	if n != 0 {
		t.Fatalf("a connection accepted after close was tracked: %d", n)
	}
	if err := s.Stop(); err != nil {
		t.Fatal(err)
	}
}

func TestCaptureStopReportsUnremovableSocketPath(t *testing.T) {
	path := shortSocketPath(t)
	s, err := NewCaptureSocket(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	occupyWithDirectory(t, path)
	if err := s.Stop(); err == nil {
		t.Fatal("Stop must report a socket path it cannot remove")
	}
}

func TestPamListenerStartChmodFailure(t *testing.T) {
	failChmod(t)
	l, err := NewPamNotifyListener(shortSocketPath(t), nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Start(context.Background(), nil); !errors.Is(err, errSeam) {
		t.Fatalf("Start = %v, want the chmod failure", err)
	}
}

func TestPamListenerAcceptAfterCloseAndUnremovablePath(t *testing.T) {
	path := shortSocketPath(t)
	l, err := NewPamNotifyListener(path, nil)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if err := l.Start(ctx, nil); err != nil {
		t.Fatal(err)
	}
	l.mu.Lock()
	l.closed = true
	l.mu.Unlock()
	dialAndExpectClose(t, path)
	l.mu.Lock()
	l.closed = false
	l.mu.Unlock()
	occupyWithDirectory(t, path)
	if err := l.Stop(ctx); err == nil {
		t.Fatal("Stop must report a socket path it cannot remove")
	}
}

// pamConn builds a connector that will authenticate through a fake backend as
// if running as root; the user it names does not exist, so Start stops at
// user resolution once PAM has run.
func pamConn(t *testing.T, newPam func(string) (*PamSession, error)) *PTYConnector {
	t.Helper()
	c := makeConn(t, "/bin/sh", nil, map[string]any{"username": "uterm-no-such-user", "password": "pw"})
	c.geteuid = func() int { return 0 }
	c.newPam = newPam
	return c
}

func TestStartPamLifecycle(t *testing.T) {
	ctx := context.Background()
	backend := func(h *fakeHandle) func(string) (*PamSession, error) {
		return func(service string) (*PamSession, error) {
			return NewPamSessionWithBackend(service, fakeBackend{h: h})
		}
	}

	c := pamConn(t, func(string) (*PamSession, error) { return nil, errSeam })
	if err := c.Start(ctx); !errors.Is(err, errSeam) {
		t.Fatalf("session construction failure = %v", err)
	}

	c = pamConn(t, backend(&fakeHandle{acctErr: errSeam}))
	if err := c.Start(ctx); err == nil {
		t.Fatal("an account-management refusal must stop Start")
	}

	c = pamConn(t, backend(&fakeHandle{openErr: errSeam}))
	if err := c.Start(ctx); err == nil {
		t.Fatal("a session-open refusal must stop Start")
	}

	h := &fakeHandle{env: map[string]string{"PAM_X": "1"}}
	c = pamConn(t, backend(h))
	err := c.Start(ctx)
	if !IsUidMapError(err) {
		t.Fatalf("Start = %v, want the user-resolution failure after PAM succeeded", err)
	}
	if c.pam == nil || c.pam.Env()["PAM_X"] != "1" {
		t.Fatal("the PAM session and its environment must be kept")
	}
}

func TestStartCaptureSetupFailures(t *testing.T) {
	ctx := context.Background()
	t.Run("temp dir", func(t *testing.T) {
		t.Setenv("TMPDIR", filepath.Join(t.TempDir(), "missing"))
		c := makeConn(t, "/bin/true", nil, map[string]any{"inject": true})
		if err := c.Start(ctx); err == nil {
			t.Fatal("expected the capture temp dir failure")
		}
	})
	t.Run("relative socket path", func(t *testing.T) {
		t.Chdir(t.TempDir())
		if err := os.Mkdir("rel", 0o755); err != nil {
			t.Fatal(err)
		}
		t.Setenv("TMPDIR", "rel")
		c := makeConn(t, "/bin/true", nil, map[string]any{"inject": true})
		if err := c.Start(ctx); err == nil || !strings.Contains(err.Error(), "absolute") {
			t.Fatalf("Start = %v, want the socket path refused", err)
		}
	})
	t.Run("socket path too long to bind", func(t *testing.T) {
		long := filepath.Join(t.TempDir(), strings.Repeat("d", 120))
		if err := os.MkdirAll(long, 0o755); err != nil {
			t.Fatal(err)
		}
		t.Setenv("TMPDIR", long)
		c := makeConn(t, "/bin/true", nil, map[string]any{"inject": true})
		if err := c.Start(ctx); err == nil {
			t.Fatal("expected the bind to fail on an over-long socket path")
		}
	})
}

func TestStartSpawnFailure(t *testing.T) {
	c := makeConn(t, filepath.Join(t.TempDir(), "missing-binary"), nil, nil)
	if err := c.Start(context.Background()); err == nil {
		t.Fatal("expected the spawn to fail")
	}
	if c.connected {
		t.Fatal("a failed spawn must not leave the connector connected")
	}
}

// Dropping to another user only works as root; otherwise Start must fail
// rather than run the child with the caller's privileges.
func TestStartRunAsDropsPrivilegeOrFails(t *testing.T) {
	if _, err := user.Lookup("nobody"); err != nil {
		t.Fatalf("this test needs the standard nobody user: %v", err)
	}
	c := makeConn(t, "/bin/sh", []string{"-c", "id -u"}, map[string]any{"run_as": "nobody"})
	err := c.Start(context.Background())
	if os.Geteuid() != 0 {
		if err == nil {
			_ = c.Stop(context.Background())
			t.Fatal("a non-root server must not be able to switch users")
		}
		return
	}
	if err != nil {
		t.Fatalf("root must be able to drop to nobody: %v", err)
	}
	defer func() { _ = c.Stop(context.Background()) }()
	u, _ := user.Lookup("nobody")
	waitForScreen(t, c, u.Uid)
}

func TestBuildEnvInjectsCaptureLibrary(t *testing.T) {
	origLib, origDarwin := getCaptureLibPath, isDarwin
	t.Cleanup(func() { getCaptureLibPath, isDarwin = origLib, origDarwin })
	getCaptureLibPath = func() string { return "/opt/libcap.so" }
	c := makeConn(t, "/bin/true", nil, nil)

	isDarwin = func() bool { return false }
	env := strings.Join(c.buildEnv(nil, nil, "/tmp/cap.sock"), "\n")
	if !strings.Contains(env, "LD_PRELOAD=/opt/libcap.so") || strings.Contains(env, "DYLD_INSERT_LIBRARIES") {
		t.Fatalf("linux env = %q", env)
	}
	isDarwin = func() bool { return true }
	env = strings.Join(c.buildEnv(nil, nil, "/tmp/cap.sock"), "\n")
	if !strings.Contains(env, "DYLD_INSERT_LIBRARIES=/opt/libcap.so") ||
		!strings.Contains(env, "DYLD_FORCE_FLAT_NAMESPACE=1") || strings.Contains(env, "LD_PRELOAD") {
		t.Fatalf("darwin env = %q", env)
	}
}

func TestSpawnPTYEchoFailureReapsChild(t *testing.T) {
	orig := setNoEcho
	setNoEcho = func(int) error { return errSeam }
	t.Cleanup(func() { setNoEcho = orig })
	child, err := spawnPTY("/bin/sh", []string{"-c", "sleep 30"}, nil, nil, 80, 24)
	if !errors.Is(err, errSeam) || child != nil {
		t.Fatalf("spawnPTY = %v, %v; want the echo failure", child, err)
	}
}

func TestSupplementaryGroups(t *testing.T) {
	me := &ResolvedUser{UID: os.Getuid(), GID: os.Getgid()}
	orig := userGroupIDs
	t.Cleanup(func() { userGroupIDs = orig })

	userGroupIDs = func(*user.User) ([]string, error) { return nil, errSeam }
	if got := supplementaryGroups(me); got != nil {
		t.Fatalf("groups = %v, want nil on a lookup failure", got)
	}

	gid := strconv.Itoa(me.GID)
	userGroupIDs = func(*user.User) ([]string, error) { return []string{gid, "not-a-gid", "4242", "4242"}, nil }
	got := supplementaryGroups(me)
	if len(got) != 2 || int(got[0]) != me.GID || got[1] != 4242 {
		t.Fatalf("groups = %v, want primary then 4242, deduplicated", got)
	}
}

func TestResolveNamedAndNumericUsers(t *testing.T) {
	nobody, err := user.Lookup("nobody")
	if err != nil {
		t.Fatalf("this test needs the standard nobody user: %v", err)
	}
	m := NewUidMap(nil, false)
	r, err := m.Resolve("nobody", ResolveOpts{})
	if err != nil || strconv.Itoa(r.UID) != nobody.Uid || r.Name != "nobody" {
		t.Fatalf("Resolve(nobody) = %+v, %v", r, err)
	}
	gid := 4242
	r, err = m.Resolve("nobody", ResolveOpts{RunAsGID: &gid})
	if err != nil || r.GID != 4242 {
		t.Fatalf("Resolve with gid override = %+v, %v", r, err)
	}
	r, err = m.Resolve("", ResolveOpts{RunAs: "4243"})
	if err != nil || r.UID != 4243 {
		t.Fatalf("Resolve(run_as uid) = %+v, %v", r, err)
	}
}

func TestShellFromPasswd(t *testing.T) {
	orig := passwdPath
	t.Cleanup(func() { passwdPath = orig })

	passwdPath = filepath.Join(t.TempDir(), "missing")
	if got := shellFromPasswd(0, "root"); got != "" {
		t.Fatalf("missing passwd = %q", got)
	}

	fixture := filepath.Join(t.TempDir(), "passwd")
	body := "# comment\n\nshort:line\nalice:x:1001:1001::/home/alice:/bin/zsh\n"
	if err := os.WriteFile(fixture, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	passwdPath = fixture
	if got := shellFromPasswd(1001, ""); got != "/bin/zsh" {
		t.Fatalf("shell = %q", got)
	}
}
