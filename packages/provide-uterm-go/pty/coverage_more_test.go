//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"bytes"
	"context"
	"net"
	"os"
	"os/exec"
	"testing"
	"time"
)

// nonexistentSocketPath is an absolute path whose parent directory does not
// exist, so ValidateSocketPath passes but net.Listen fails at bind time.
const nonexistentSocketPath = "/nonexistent_dir_xyzzy_uterm/x.sock"

// --- capture.go ---

func TestCaptureStartListenError(t *testing.T) {
	// Absolute path in a missing directory: passes validation, fails to bind.
	s, err := NewCaptureSocket(nonexistentSocketPath)
	if err != nil {
		t.Fatalf("construct: %v", err)
	}
	if err := s.Start(); err == nil {
		_ = s.Stop()
		t.Fatal("expected bind error for missing directory")
	}
}

func TestCaptureStopClosesLiveConn(t *testing.T) {
	path := shortSocketPath(t)
	s, _ := NewCaptureSocket(path)
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	conn, err := net.Dial("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = conn.Close() }()
	// Wait until the accept loop has registered the live connection so Stop's
	// conns-close loop actually iterates over it.
	waitForConnCount(t, func() int {
		s.mu.Lock()
		defer s.mu.Unlock()
		return len(s.conns)
	}, 1)
	if err := s.Stop(); err != nil {
		t.Fatalf("stop with live conn: %v", err)
	}
}

func TestCaptureHandleConnShortBody(t *testing.T) {
	path := shortSocketPath(t)
	s, _ := NewCaptureSocket(path)
	// Header announces 8 body bytes but only 3 are supplied → io.ReadFull on the
	// body errors and the connection is dropped with nothing enqueued.
	frame := makeFrame(ChannelStdout, []byte("12345678"))
	truncated := frame[:headerSize+3]
	s.handleConn(bytes.NewReader(truncated))
	if s.QueueLen() != 0 {
		t.Fatalf("short body should enqueue nothing, got %d", s.QueueLen())
	}
}

// --- captureconnector.go ---

func TestCaptureConnectorTimeoutCoercion(t *testing.T) {
	cFloat, err := NewCaptureConnector("s", "d", map[string]any{
		"socket_path": "/tmp/x.sock", "connect_timeout_s": 2.5,
	})
	if err != nil || cFloat.connectTimeout != 2.5 {
		t.Fatalf("float timeout: %v err=%v", cFloat, err)
	}
	cInt, err := NewCaptureConnector("s", "d", map[string]any{
		"socket_path": "/tmp/x.sock", "connect_timeout_s": 7,
	})
	if err != nil || cInt.connectTimeout != 7.0 {
		t.Fatalf("int timeout: %v err=%v", cInt, err)
	}
}

func TestCaptureConnectorStartBadSocketPath(t *testing.T) {
	c, err := NewCaptureConnector("s", "d", map[string]any{"socket_path": "relative.sock"})
	if err != nil {
		t.Fatalf("construct: %v", err)
	}
	if err := c.Start(context.Background()); err == nil {
		_ = c.Stop(context.Background())
		t.Fatal("expected NewCaptureSocket validation error for relative path")
	}
}

func TestCaptureConnectorStartBindError(t *testing.T) {
	c, err := NewCaptureConnector("s", "d", map[string]any{"socket_path": nonexistentSocketPath})
	if err != nil {
		t.Fatalf("construct: %v", err)
	}
	if err := c.Start(context.Background()); err == nil {
		_ = c.Stop(context.Background())
		t.Fatal("expected bind error for missing directory")
	}
}

func TestCaptureConnectorStopWithoutStart(t *testing.T) {
	c := newCaptureConn(t, nil)
	if err := c.Stop(context.Background()); err != nil {
		t.Fatalf("stop without start should be a no-op: %v", err)
	}
}

func TestCaptureConnectorBufferAndConnectLogCaps(t *testing.T) {
	c := newCaptureConn(t, nil)
	if err := c.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Stop(context.Background()) }()

	// One stdout frame larger than the buffer cap → buffer trimmed to the cap.
	big := bytes.Repeat([]byte("a"), captureBufferCap+2048)
	c.capture.handleConn(bytes.NewReader(makeFrame(ChannelStdout, big)))
	// 101 connect frames → connectLog trimmed to the last 100.
	var connBlob []byte
	for i := 0; i < 101; i++ {
		connBlob = append(connBlob, makeFrame(ChannelConnect, []byte("10.0.0.1:1"))...)
	}
	c.capture.handleConn(bytes.NewReader(connBlob))

	c.PollMessages() // drains everything and applies both caps
	c.mu.Lock()
	bufLen := len(c.buffer)
	logLen := len(c.connectLog)
	c.mu.Unlock()
	if bufLen != captureBufferCap {
		t.Fatalf("buffer len = %d, want %d", bufLen, captureBufferCap)
	}
	if logLen != 100 {
		t.Fatalf("connectLog len = %d, want 100", logLen)
	}
}

func TestCaptureConnectorForwardStdinDialError(t *testing.T) {
	// stdin path points at a socket that does not exist → Dial fails, no panic.
	c := newCaptureConn(t, map[string]any{"stdin_socket_path": nonexistentSocketPath})
	if err := c.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Stop(context.Background()) }()
	if got := c.HandleInput(context.Background(), "hi"); got != nil {
		t.Fatalf("dial failure should return nil, got %+v", got)
	}
	c.mu.Lock()
	writer := c.stdinWriter
	c.mu.Unlock()
	if writer != nil {
		t.Fatal("no writer should be retained after a dial failure")
	}
}

func TestCaptureConnectorForwardStdinReconnectsOnWriteError(t *testing.T) {
	stdinPath := shortSocketPath(t)
	stdinSock, err := NewCaptureSocket(stdinPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := stdinSock.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = stdinSock.Stop() }()

	c := newCaptureConn(t, map[string]any{"stdin_socket_path": stdinPath})
	if err := c.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Stop(context.Background()) }()

	// Seed a stale (already-closed) writer: the first write fails, forcing the
	// reconnect-and-retry branch to close it and dial a fresh connection.
	stale, err := net.Dial("unix", stdinPath)
	if err != nil {
		t.Fatal(err)
	}
	_ = stale.Close()
	c.mu.Lock()
	c.stdinWriter = stale
	c.mu.Unlock()

	c.HandleInput(context.Background(), "retry")
	c.mu.Lock()
	writer := c.stdinWriter
	c.mu.Unlock()
	if writer == nil || writer == stale {
		t.Fatalf("expected a fresh writer after reconnect, got %v", writer)
	}
}

// --- connector.go ---

func TestConnectorNewEnvError(t *testing.T) {
	_, err := NewPTYConnector("s", "d", map[string]any{"command": "/bin/sh", "env": []int{1}})
	assertErr(t, err, "env must be a map")
}

func TestConnectorNewRunAsGIDError(t *testing.T) {
	_, err := NewPTYConnector("s", "d", map[string]any{"command": "/bin/sh", "run_as_gid": "notanint"})
	assertErr(t, err, "run_as_gid must be an integer")
}

func TestConnectorStartPamRequiresRealBackend(t *testing.T) {
	// geteuid seam reports root so the PAM branch runs; the fail-closed stub
	// backend then refuses to authenticate (no real libpam).
	c := makeConn(t, "/bin/echo", nil, map[string]any{"username": "someuser", "password": "pw"})
	c.geteuid = func() int { return 0 }
	err := c.Start(context.Background())
	assertErr(t, err, "libpam not available")
}

func TestConnectorStartPamRequiresRoot(t *testing.T) {
	c := makeConn(t, "/bin/echo", nil, map[string]any{"username": "someuser", "password": "pw"})
	c.geteuid = func() int { return 1000 } // non-root
	assertErr(t, c.Start(context.Background()), "requires the server to run as root")
}

func TestConnectorStartResolvesRunAsThenSpawns(t *testing.T) {
	// run_as = our own username drives the resolve path. Dropping privileges to a
	// Credential requires setgroups/setuid (root only), so a non-root runner sees
	// spawnPTY fail with EPERM — either way the resolve + spawn code runs.
	u, _, _ := currentUser(t)
	c := makeConn(t, "/bin/echo", []string{"hi"}, map[string]any{"run_as": u.Username})
	err := c.Start(context.Background())
	if err == nil {
		// Root runner: the child actually launched — tear it down.
		defer func() { _ = c.Stop(context.Background()) }()
		if !c.IsConnected() {
			t.Fatal("root spawn should be connected")
		}
		return
	}
	// Non-root: privilege drop refused before the child ran.
	if c.IsConnected() {
		t.Fatal("failed spawn must not report connected")
	}
}

func TestConnectorStartResolveError(t *testing.T) {
	// run_as names a user that does not exist → the resolve step fails and Start
	// surfaces the error before any spawn.
	c := makeConn(t, "/bin/echo", nil, map[string]any{"run_as": "__no_such_user_xyzzy__"})
	assertErr(t, c.Start(context.Background()), "no such OS user")
	if c.IsConnected() {
		t.Fatal("failed resolve must not report connected")
	}
}

func TestConnectorStartInjectCreatesCaptureSocket(t *testing.T) {
	t.Setenv("TMPDIR", "/tmp") // keep the capture socket path under the ~104B unix limit
	c := makeConn(t, "/bin/echo", []string{"hi"}, map[string]any{"inject": true})
	if err := c.Start(context.Background()); err != nil {
		t.Fatalf("start with inject: %v", err)
	}
	c.mu.Lock()
	cs := c.captureSocket
	tmp := c.captureTmpDir
	c.mu.Unlock()
	if cs == nil || tmp == "" {
		t.Fatal("inject should create a capture socket + temp dir")
	}
	if _, err := os.Stat(cs.Path()); err != nil {
		t.Fatalf("capture socket file missing: %v", err)
	}
	waitForScreen(t, c, "hi")
	if err := c.Stop(context.Background()); err != nil {
		t.Fatalf("stop: %v", err)
	}
	if _, err := os.Stat(tmp); !os.IsNotExist(err) {
		t.Fatalf("temp dir should be removed after stop, err=%v", err)
	}
}

func TestConnectorStopMasterWithoutChild(t *testing.T) {
	c := makeConn(t, "/bin/cat", nil, nil)
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = r.Close() }()
	// child==nil but master!=nil exercises the master-only close branch.
	c.master = w
	c.connected = true
	if err := c.Stop(context.Background()); err != nil {
		t.Fatalf("stop: %v", err)
	}
	if c.IsConnected() {
		t.Fatal("should be disconnected after stop")
	}
}

func TestConnectorStopClosesPamSession(t *testing.T) {
	c := makeConn(t, "/bin/cat", nil, nil)
	pam, err := NewPamSession("provide-uterm")
	if err != nil {
		t.Fatal(err)
	}
	c.pam = pam // no child/master: exercises the pam-cleanup branch of Stop
	if err := c.Stop(context.Background()); err != nil {
		t.Fatalf("stop: %v", err)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.pam != nil {
		t.Fatal("pam should be cleared after stop")
	}
}

func TestReapChildNilWaitDoneReturns(t *testing.T) {
	c := makeConn(t, "/bin/cat", nil, nil)
	// A nil waitDone means Start never launched a wait goroutine → immediate return.
	c.reapChild(&spawnedChild{}, nil)
}

func TestConnectorReapEscalatesToKill(t *testing.T) {
	// Drive reapChild directly with a real, long-lived subprocess that does NOT
	// exit on its own within the grace window. (Going through a PTY child is
	// unreliable here: closing the master revokes the controlling terminal and
	// the shell exits before the grace timeout, so the wait-completes path is
	// taken instead of the SIGKILL escalation we want to exercise.)
	cmd := exec.Command("/bin/sh", "-c", "sleep 30")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	waitDone := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(waitDone)
	}()

	orig := stopGraceWindow
	stopGraceWindow = 80 * time.Millisecond
	defer func() { stopGraceWindow = orig }()

	c := makeConn(t, "/bin/cat", nil, nil)
	done := make(chan struct{})
	go func() {
		// Child is alive and waitDone stays open past the grace window, so
		// reapChild must time out and SIGKILL it, then block on the reap.
		c.reapChild(&spawnedChild{cmd: cmd}, waitDone)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("reapChild did not escalate to SIGKILL in time")
	}
	select {
	case <-waitDone:
	default:
		t.Fatal("child should have been reaped")
	}
}

// --- connector_io.go ---

func TestHandleInputWriteErrorMarksDisconnected(t *testing.T) {
	c := makeConn(t, "/bin/cat", nil, nil)
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = r.Close() }()
	_ = w.Close() // writing to a closed file errors deterministically
	c.master = w
	c.connected = true
	if !anyType(c.HandleInput(context.Background(), "data"), "snapshot") {
		t.Fatal("handle_input should still return a snapshot on write error")
	}
	if c.IsConnected() {
		t.Fatal("a write error should mark the connector disconnected")
	}
}

// --- echo.go ---

func TestDisableEchoNonTTYErrors(t *testing.T) {
	// A pipe fd is not a terminal, so the tcgetattr ioctl fails with ENOTTY —
	// exercising disableEcho's error path without needing to fault a real PTY.
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = r.Close(); _ = w.Close() }()
	if err := disableEcho(int(r.Fd())); err == nil {
		t.Fatal("disableEcho on a non-tty fd should error")
	}
}

// --- pam.go ---

type nilHandleBackend struct{}

func (nilHandleBackend) Authenticate(service, username, password string) (pamHandle, error) {
	return nil, nil // authenticated but no live handle (defensive stub path)
}

func TestPamNilHandleLifecycle(t *testing.T) {
	p, err := NewPamSessionWithBackend("provide-uterm", nilHandleBackend{})
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Authenticate("alice", "pw"); err != nil {
		t.Fatalf("authenticate: %v", err)
	}
	if err := p.AcctMgmt(); err != nil { // handle==nil → nil
		t.Fatalf("acct: %v", err)
	}
	if err := p.OpenSession(); err != nil { // handle==nil → sessionOpen, no env
		t.Fatalf("open: %v", err)
	}
	p.CloseSession() // handle==nil → no handle close, must not panic
}

// --- pamlistener.go ---

func TestListenerStartListenError(t *testing.T) {
	l, err := NewPamNotifyListener(nonexistentSocketPath, nil)
	if err != nil {
		t.Fatalf("construct: %v", err)
	}
	if err := l.Start(context.Background(), func(context.Context, PamEvent) {}); err == nil {
		_ = l.Stop(context.Background())
		t.Fatal("expected bind error for missing directory")
	}
}

func TestListenerStopClosesLiveConn(t *testing.T) {
	path := shortSocketPath(t)
	l, _ := NewPamNotifyListener(path, nil)
	if err := l.Start(context.Background(), func(context.Context, PamEvent) {}); err != nil {
		t.Fatal(err)
	}
	conn, err := net.Dial("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = conn.Close() }()
	waitForConnCount(t, func() int {
		l.mu.Lock()
		defer l.mu.Unlock()
		return len(l.conns)
	}, 1)
	if err := l.Stop(context.Background()); err != nil {
		t.Fatalf("stop with live conn: %v", err)
	}
}

func TestListenerBadJSONLineSkipped(t *testing.T) {
	path := shortSocketPath(t)
	l, _ := NewPamNotifyListener(path, nil)
	got := make(chan PamEvent, 4)
	if err := l.Start(context.Background(), func(_ context.Context, ev PamEvent) { got <- ev }); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = l.Stop(context.Background()) }()

	conn, err := net.Dial("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	// An unparseable line is skipped; the following valid line still delivers.
	_, _ = conn.Write([]byte("not json at all\n"))
	_, _ = conn.Write([]byte(`{"event":"open","username":"ok","pid":3}` + "\n"))
	_ = conn.Close()

	select {
	case ev := <-got:
		if ev.Username != "ok" {
			t.Fatalf("unexpected event %+v", ev)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("valid event after a bad line was not delivered")
	}
}

func TestStringOrEmptyAndIntOrZeroDirect(t *testing.T) {
	// Non-nil, non-string coerces via fmt (the default branch).
	if got := stringOrEmpty(123); got != "123" {
		t.Fatalf("stringOrEmpty(123) = %q", got)
	}
	if got := stringOrEmpty(nil); got != "" {
		t.Fatalf("stringOrEmpty(nil) = %q", got)
	}
	// A native int (not a JSON float64) hits the int branch.
	if got := intOrZero(5); got != 5 {
		t.Fatalf("intOrZero(int 5) = %d", got)
	}
}

// --- passwd.go ---

func TestLookupShellFallsBackToDefault(t *testing.T) {
	// An unknown uid/name is absent from /etc/passwd; with SHELL unset the
	// hard-coded default is returned.
	if old, ok := os.LookupEnv("SHELL"); ok {
		_ = os.Unsetenv("SHELL")
		t.Cleanup(func() { _ = os.Setenv("SHELL", old) })
	}
	if got := lookupShell(999999999, "__no_such_user_xyzzy__"); got != defaultShell {
		t.Fatalf("lookupShell fallback = %q, want %q", got, defaultShell)
	}
}

func TestLookupShellUsesEnvWhenPasswdMisses(t *testing.T) {
	t.Setenv("SHELL", "/opt/custom/sh")
	if got := lookupShell(999999999, "__no_such_user_xyzzy__"); got != "/opt/custom/sh" {
		t.Fatalf("lookupShell env = %q", got)
	}
}

// --- spawn.go ---

func TestSpawnPTYWithCredential(t *testing.T) {
	// Building the Credential + resolving supplementary groups runs regardless of
	// privilege; the actual setgroups/setuid drop needs root, so a non-root runner
	// gets an error from StartWithAttrs (both the Credential block and the error
	// return are exercised).
	u, uid, gid := currentUser(t)
	resolved := &ResolvedUser{UID: uid, GID: gid, Home: u.HomeDir, Shell: "/bin/sh", Name: u.Username}
	child, err := spawnPTY("/bin/echo", []string{"hi"}, os.Environ(), resolved, 80, 24)
	if err != nil {
		return // non-root: privilege drop refused, as expected
	}
	// Root runner: the child launched — reap it.
	_ = child.master.Close()
	if child.cmd.Process != nil {
		_ = child.cmd.Process.Kill()
		_ = child.cmd.Wait()
	}
}

func TestSupplementaryGroupsUnknownUID(t *testing.T) {
	// An unresolvable uid makes user.LookupId fail → nil (setgroups fallback).
	if groups := supplementaryGroups(&ResolvedUser{UID: 999999999, GID: 999999999, Name: "x"}); groups != nil {
		t.Fatalf("unknown uid should yield nil groups, got %+v", groups)
	}
}

// --- uidmap.go ---

func TestFromUIDUnknownSyntheticWithExplicitGID(t *testing.T) {
	// allowRoot avoids the privileged-uid guard for this synthetic-user check.
	r, err := NewUidMap(nil, true).Resolve("", ResolveOpts{RunAsUID: intPtr(999999999), RunAsGID: intPtr(4242)})
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if r.UID != 999999999 || r.GID != 4242 || r.Home != "/" || r.Shell != defaultShell {
		t.Fatalf("synthetic-with-gid mismatch: %+v", r)
	}
}

func TestResolveSpecGIDParseError(t *testing.T) {
	// Valid uid, non-numeric gid in the "uid:gid" spec → gid parse error.
	_, err := NewUidMap(nil, true).Resolve("anything", ResolveOpts{RunAs: "1000:notagid"})
	assertErr(t, err, "invalid gid in spec")
}

func TestResolveNameWithGIDOverride(t *testing.T) {
	u, uid, gid := currentUser(t)
	// A name spec (non-numeric, no colon) resolves via fromUserRecord; the
	// RunAsGID override replaces the record's primary gid.
	r, err := NewUidMap(nil, true).Resolve("anything", ResolveOpts{RunAs: u.Username, RunAsGID: intPtr(gid)})
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if r.UID != uid || r.GID != gid || r.Name != u.Username {
		t.Fatalf("name+gid override mismatch: %+v", r)
	}
}

func TestResolveNameToRootRejected(t *testing.T) {
	// A table entry naming "root" resolves through fromUserRecord, whose
	// privilege check rejects the privileged uid by default.
	m := NewUidMap(map[string]string{"appuser": "root"}, false)
	_, err := m.Resolve("appuser", ResolveOpts{})
	assertErr(t, err, "privileged")
	if !IsUidMapError(err) {
		t.Fatalf("expected UidMapError, got %T", err)
	}
}

// --- shared helper ---

// waitForConnCount polls count until it reaches want or the deadline passes.
func waitForConnCount(t *testing.T, count func() int, want int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if count() >= want {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("connection count never reached %d (got %d)", want, count())
}
