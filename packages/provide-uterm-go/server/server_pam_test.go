//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"sync/atomic"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/pty"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

var pamSockCounter atomic.Int64

func pamSocketPath(t *testing.T) string {
	t.Helper()
	p := fmt.Sprintf("/tmp/utpam-%d-%d.sock", os.Getpid(), pamSockCounter.Add(1))
	t.Cleanup(func() { _ = os.Remove(p) })
	return p
}

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func strptr(s string) *string { return &s }

func newPamIntegration(cfg serverconfig.PamConfig, reg *fakeRegistry) *PamIntegration {
	return NewPamIntegration(cfg, reg, nil, quietLogger())
}

func TestPamRunNoOpWhenUnset(t *testing.T) {
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{}, reg)
	if err := pi.Run(context.Background()); err != nil {
		t.Fatalf("Run should no-op, got %v", err)
	}
}

func TestPamSessionIDAndSlug(t *testing.T) {
	cases := []struct {
		ev   pty.PamEvent
		want string
	}{
		// Parity with Python _tty_slug: the basename is the segment after the last
		// "/", so "/dev/pts/3" → "3" (the "pts-3" docstring is aspirational).
		{pty.PamEvent{Username: "alice", TTY: "/dev/pts/3"}, "pam-alice-3"},
		{pty.PamEvent{Username: "bob", TTY: "", PID: 42}, "pam-bob-tty-42"},
		{pty.PamEvent{Username: "eve", TTY: "tty1"}, "pam-eve-tty1"},
		// Capture sessions key on the pid, never the tty: pam_uterm.so publishes
		// UTERM_CAPTURE_SOCKET per-pid, and a capture close event carries the tty
		// too, so a tty-keyed id would not match the id minted at open.
		{pty.PamEvent{Username: "carol", TTY: "/dev/pts/4", PID: 7, Mode: "capture"}, "pam-carol-capture-7"},
		// The socket alone is enough — mode may be absent on a close event.
		{pty.PamEvent{Username: "dan", TTY: "/dev/pts/5", PID: 9, CaptureSocket: "/run/uterm-cap-9.sock"}, "pam-dan-capture-9"},
	}
	for _, c := range cases {
		if got := pamSessionID(c.ev); got != c.want {
			t.Errorf("pamSessionID(%+v)=%q want %q", c.ev, got, c.want)
		}
	}
}

func TestPamOnOpenNotifyCreatesSession(t *testing.T) {
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "notify", AutoSession: true, AutoSessionCommand: "/bin/zsh"}, reg)
	pi.onOpen(context.Background(), pty.PamEvent{Event: "open", Username: "alice", TTY: "/dev/pts/1", Mode: "notify"})

	if len(reg.created) != 1 {
		t.Fatalf("want 1 create, got %d", len(reg.created))
	}
	p := reg.created[0]
	if p["session_id"] != "pam-alice-1" || p["connector_type"] != "pty" || p["visibility"] != "operator" {
		t.Fatalf("unexpected payload: %+v", p)
	}
	cc := p["connector_config"].(map[string]any)
	if cc["command"] != "/bin/zsh" || cc["username"] != "alice" {
		t.Fatalf("unexpected connector_config: %+v", cc)
	}
}

func TestPamOnOpenNotifyDisabled(t *testing.T) {
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "notify", AutoSession: false}, reg)
	pi.onOpen(context.Background(), pty.PamEvent{Event: "open", Username: "alice", Mode: "notify"})
	if len(reg.created) != 0 {
		t.Fatalf("auto_session off: want 0 creates, got %d", len(reg.created))
	}
}

func TestPamOnOpenCaptureConfined(t *testing.T) {
	dir := t.TempDir()
	sock := dir + "/cap.sock"
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "capture", CaptureSocketDir: &dir}, reg)
	pi.onOpen(context.Background(), pty.PamEvent{
		Event: "open", Username: "carol", TTY: "/dev/pts/9", Mode: "capture", CaptureSocket: sock,
	})
	if len(reg.created) != 1 {
		t.Fatalf("want 1 create, got %d", len(reg.created))
	}
	p := reg.created[0]
	if p["connector_type"] != "pty_capture" {
		t.Fatalf("want pty_capture, got %v", p["connector_type"])
	}
	if p["connector_config"].(map[string]any)["socket_path"] != sock {
		t.Fatalf("socket_path not threaded: %+v", p)
	}
}

func TestPamCaptureSocketOutsideDirRefused(t *testing.T) {
	dir := t.TempDir()
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "capture", CaptureSocketDir: &dir}, reg)
	pi.onOpen(context.Background(), pty.PamEvent{
		Event: "open", Username: "mallory", Mode: "capture", CaptureSocket: "/etc/evil.sock",
	})
	if len(reg.created) != 0 {
		t.Fatalf("out-of-tree capture socket must NOT create a session, got %d", len(reg.created))
	}
}

func TestPamCaptureNoConfinementWhenNoBase(t *testing.T) {
	reg := newFakeRegistry()
	// No capture_socket_dir and no notify_socket → no confinement (allowed).
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "capture"}, reg)
	pi.onOpen(context.Background(), pty.PamEvent{
		Event: "open", Username: "u", Mode: "capture", CaptureSocket: "/anywhere/x.sock",
	})
	if len(reg.created) != 1 {
		t.Fatalf("want 1 create with no confinement, got %d", len(reg.created))
	}
}

// TestPamOnCloseDeletes pins the delete-on-close contract: a PAM session is
// ephemeral, so closing it must remove the definition from the registry rather
// than leave a stopped shell behind. Port of _on_close's registry.delete_session.
func TestPamOnCloseDeletes(t *testing.T) {
	reg := newFakeRegistry()
	reg.add("pam-dave-2", "dave", "operator")
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "notify"}, reg)
	pi.onClose(context.Background(), pty.PamEvent{Event: "close", Username: "dave", TTY: "/dev/pts/2"})
	if len(reg.deleted) != 1 || reg.deleted[0] != "pam-dave-2" {
		t.Fatalf("want delete of pam-dave-2, got %v", reg.deleted)
	}
	if _, ok := reg.GetDefinition(context.Background(), "pam-dave-2"); ok {
		t.Fatal("pam-dave-2 must not survive in the registry after close")
	}
}

// TestPamCaptureOpenCloseSameID pins that a capture session opened and closed
// with a tty present resolves to one id, so the close deletes what open created.
func TestPamCaptureOpenCloseSameID(t *testing.T) {
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "capture"}, reg)
	open := pty.PamEvent{
		Event: "open", Username: "carol", TTY: "/dev/pts/4", PID: 7,
		Mode: "capture", CaptureSocket: "/run/uterm-cap-7.sock",
	}
	pi.onOpen(context.Background(), open)
	if len(reg.created) != 1 {
		t.Fatalf("want 1 create, got %d", len(reg.created))
	}
	created, _ := reg.created[0]["session_id"].(string)

	pi.onClose(context.Background(), pty.PamEvent{
		Event: "close", Username: "carol", TTY: "/dev/pts/4", PID: 7, Mode: "capture",
	})
	if len(reg.deleted) != 1 || reg.deleted[0] != created {
		t.Fatalf("close deleted %v, want the created id %q", reg.deleted, created)
	}
}

func TestPamRelayForward(t *testing.T) {
	type cap struct {
		path, auth string
	}
	got := make(chan cap, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got <- cap{path: r.URL.Path, auth: r.Header.Get("Authorization")}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{
		Mode: "notify", RelayURL: strptr(srv.URL), RelayToken: strptr("tok"),
	}, reg)
	pi.forwardToRelay(context.Background(), map[string]any{"event": "open"})
	select {
	case c := <-got:
		if c.path != "/api/pam-events" || c.auth != "Bearer tok" {
			t.Fatalf("unexpected relay call: %+v", c)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("relay never received POST")
	}
}

func TestPamRelayForwardEgressBlocked(t *testing.T) {
	// A metadata relay URL must be blocked before the POST; there is nothing to
	// observe except that it returns without panicking (best-effort no-op).
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{
		Mode: "notify", RelayURL: strptr("http://169.254.169.254"), RelayToken: strptr("tok"),
	}, reg)
	pi.forwardToRelay(context.Background(), map[string]any{"event": "open"})
	// No assertion beyond no-hang/no-panic; the metadata guard swallowed it.
}

func TestPamHandleDispatch(t *testing.T) {
	reg := newFakeRegistry()
	reg.add("pam-x-1", "x", "operator")
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "notify", AutoSession: true}, reg)
	pi.handle(context.Background(), pty.PamEvent{Event: "open", Username: "x", TTY: "/dev/pts/1"})
	pi.handle(context.Background(), pty.PamEvent{Event: "close", Username: "x", TTY: "/dev/pts/1"})
	pi.handle(context.Background(), pty.PamEvent{Event: "weird", Username: "x"}) // ignored
	if len(reg.created) != 1 || len(reg.deleted) != 1 {
		t.Fatalf("dispatch: created=%d deleted=%d", len(reg.created), len(reg.deleted))
	}
}

func TestPamSafeCreateError(t *testing.T) {
	reg := newFakeRegistry()
	reg.createErr = errFixed("nope")
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "notify", AutoSession: true}, reg)
	// Should not panic; the error is logged and swallowed.
	pi.onOpen(context.Background(), pty.PamEvent{Event: "open", Username: "a", TTY: "/dev/pts/1"})
}

func TestPamCreateNotifyDefaultCommand(t *testing.T) {
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "notify", AutoSession: true}, reg) // no AutoSessionCommand
	pi.createNotifySession(context.Background(), pty.PamEvent{Username: "a", TTY: "/dev/pts/1"})
	cc := reg.created[0]["connector_config"].(map[string]any)
	if cc["command"] != "/bin/bash" {
		t.Fatalf("default command = %v, want /bin/bash", cc["command"])
	}
}

func TestPamOnOpenAndCloseWithRelay(t *testing.T) {
	var events, tunnels int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/pam-events":
			atomic.AddInt32(&events, 1)
		case "/api/tunnels":
			atomic.AddInt32(&tunnels, 1)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	reg := newFakeRegistry()
	reg.add("pam-a-1", "a", "operator")
	pi := newPamIntegration(serverconfig.PamConfig{
		Mode: "notify", AutoSession: true, RelayURL: strptr(srv.URL), RelayToken: strptr("t"),
	}, reg)
	pi.onOpen(context.Background(), pty.PamEvent{Event: "open", Username: "a", TTY: "/dev/pts/1"})
	pi.onClose(context.Background(), pty.PamEvent{Event: "close", Username: "a", TTY: "/dev/pts/1"})
	// open + close each POST /api/pam-events; open also POSTs /api/tunnels. The
	// fakeRegistry exposes no connector, so no bridge is started (the tunnel
	// response body is empty → parse fails → logged and swallowed).
	if atomic.LoadInt32(&events) != 2 {
		t.Fatalf("want 2 pam-event POSTs (open+close), got %d", events)
	}
	if atomic.LoadInt32(&tunnels) != 1 {
		t.Fatalf("want 1 tunnel-provisioning POST on open, got %d", tunnels)
	}
}

func TestPamRunInvalidSocketError(t *testing.T) {
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{NotifySocket: strptr("relative.sock")}, reg)
	if err := pi.Run(context.Background()); err == nil {
		t.Fatal("want error for a relative notify socket path")
	}
}

// TestPamRunListenerStartError covers the listener.Start error branch: an
// absolute path (passes ValidateSocketPath) in a nonexistent directory fails to
// bind.
func TestPamRunListenerStartError(t *testing.T) {
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{
		NotifySocket: strptr("/nonexistent-uterm-dir-xyzzy/pam.sock"),
	}, reg)
	if err := pi.Run(context.Background()); err == nil {
		t.Fatal("want bind error for a socket in a nonexistent directory")
	}
}

// TestPamOnCloseUnknownSession covers the DeleteSession-error branch: the error
// is logged, never raised, so a close for a session the registry never had is a
// no-op rather than a crash.
func TestPamOnCloseUnknownSession(t *testing.T) {
	reg := newFakeRegistry()
	reg.deleteErr = errFixed("no such session")
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "notify"}, reg)
	pi.onClose(context.Background(), pty.PamEvent{Event: "close", Username: "ghost", TTY: "/dev/pts/9"})
	// DeleteSession still recorded the attempt; the error was logged, not raised.
	if len(reg.deleted) != 1 {
		t.Fatalf("want 1 delete attempt, got %d", len(reg.deleted))
	}
}

// TestPamCaptureEmptySocket covers the empty-capture-socket early return.
func TestPamCaptureEmptySocket(t *testing.T) {
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "capture"}, reg)
	pi.createCaptureSession(context.Background(), pty.PamEvent{Username: "a", Mode: "capture"})
	if len(reg.created) != 0 {
		t.Fatalf("empty capture socket must not create, got %d", len(reg.created))
	}
}

// TestPamCaptureConfinedByNotifyParent covers the notify-socket-parent base-dir
// branch of captureSocketConfined.
func TestPamCaptureConfinedByNotifyParent(t *testing.T) {
	dir := t.TempDir()
	notify := dir + "/notify.sock"
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "capture", NotifySocket: &notify}, reg)
	pi.createCaptureSession(context.Background(), pty.PamEvent{
		Username: "a", TTY: "/dev/pts/1", Mode: "capture", CaptureSocket: dir + "/cap.sock",
	})
	if len(reg.created) != 1 {
		t.Fatalf("socket under notify parent should be allowed, got %d", len(reg.created))
	}
}

// TestPamRelayForwardPostError covers the client.Do error branch (allowed
// target, refused connection).
func TestPamRelayForwardPostError(t *testing.T) {
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{
		Mode: "notify", RelayURL: strptr("http://127.0.0.1:1"), RelayToken: strptr("t"),
	}, reg)
	pi.forwardToRelay(context.Background(), map[string]any{"event": "open"}) // logged, no panic
}

// TestPamRunEndToEnd binds the notify socket, connects, sends an open event, and
// asserts a session is created — exercising Run + the listener dispatch path.
func TestPamRunEndToEnd(t *testing.T) {
	sock := pamSocketPath(t)
	reg := newFakeRegistry()
	uids := []int{os.Getuid()}
	pi := newPamIntegration(serverconfig.PamConfig{
		NotifySocket: &sock, Mode: "notify", AutoSession: true, AutoSessionCommand: "/bin/bash",
		RequirePeerUIDs: &uids, // exercise the allowlist-copy branch in Run
	}, reg)

	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- pi.Run(ctx) }()

	// Wait for the socket to appear.
	deadline := time.Now().Add(2 * time.Second)
	for {
		if _, err := os.Stat(sock); err == nil {
			break
		}
		if time.Now().After(deadline) {
			cancel()
			t.Fatal("notify socket never bound")
		}
		time.Sleep(5 * time.Millisecond)
	}

	conn, err := net.Dial("unix", sock)
	if err != nil {
		cancel()
		t.Fatalf("dial: %v", err)
	}
	_, _ = conn.Write([]byte(`{"event":"open","username":"zed","tty":"/dev/pts/7","pid":11}` + "\n"))
	_ = conn.Close()

	// Poll for the created session.
	deadline = time.Now().Add(2 * time.Second)
	for {
		reg.mu.Lock()
		n := len(reg.created)
		reg.mu.Unlock()
		if n >= 1 {
			break
		}
		if time.Now().After(deadline) {
			cancel()
			t.Fatal("event never produced a session")
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run returned error: %v", err)
	}
}
