//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"encoding/binary"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

var captureSockCounter atomic.Int64

// shortCaptureDir returns a short trusted directory under /tmp: Unix socket
// paths are capped near 104 bytes, which t.TempDir() can overrun on macOS.
func shortCaptureDir(t *testing.T) string {
	t.Helper()
	dir := fmt.Sprintf("/tmp/utcap-%d-%d", os.Getpid(), captureSockCounter.Add(1))
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	return dir
}

// writeCaptureFrame writes one libuterm_capture frame —
// [1B channel][4B big-endian length][payload] — to the capture socket.
func writeCaptureFrame(t *testing.T, path string, channel byte, payload string) {
	t.Helper()
	conn, err := net.Dial("unix", path)
	if err != nil {
		t.Fatalf("dial capture socket: %v", err)
	}
	defer func() { _ = conn.Close() }()
	hdr := make([]byte, 5)
	hdr[0] = channel
	binary.BigEndian.PutUint32(hdr[1:], uint32(len(payload)))
	if _, err := conn.Write(append(hdr, payload...)); err != nil {
		t.Fatalf("write capture frame: %v", err)
	}
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// The PAM capture path end to end, through the real registry and the real
// connector factory: pam_uterm.so announces a capture-mode login, the
// integration creates a "pty_capture" session, and starting that session binds
// the capture socket so what the captured shell writes reaches the screen.
//
// Before pty_capture was registered, the start failed with
// `unsupported connector_type: "pty_capture"` and every capture login produced
// a session that could never run.
func TestPamCaptureSessionStartsThroughRegistry(t *testing.T) {
	dir := shortCaptureDir(t)
	notify := dir + "/notify.sock"
	capture := dir + "/cap.sock"

	reg := NewSessionRegistry(serverconfig.DefaultServerConfig())
	pi := server.NewPamIntegration(serverconfig.PamConfig{
		NotifySocket: &notify, Mode: "capture",
	}, reg, nil, slog.New(slog.NewTextHandler(io.Discard, nil)))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	runErr := make(chan error, 1)
	go func() { runErr <- pi.Run(ctx) }()

	waitFor(t, "notify socket", func() bool { _, err := os.Stat(notify); return err == nil })
	conn, err := net.Dial("unix", notify)
	if err != nil {
		t.Fatalf("dial notify: %v", err)
	}
	_, _ = fmt.Fprintf(conn,
		`{"event":"open","username":"zed","tty":"/dev/pts/7","pid":11,"mode":"capture","capture_socket":%q}`+"\n",
		capture)
	_ = conn.Close()

	const id = "pam-zed-capture-11"
	waitFor(t, "capture session created", func() bool {
		_, err := reg.GetSession(ctx, id)
		return err == nil
	})

	st, err := reg.StartSession(ctx, id)
	if err != nil {
		t.Fatalf("StartSession: %v", err)
	}
	if st.LastError != nil {
		t.Fatalf("start failed: %s", *st.LastError)
	}
	if st.LifecycleState != server.LifecycleRunning || !st.Connected {
		t.Fatalf("lifecycle=%q connected=%t, want running+connected", st.LifecycleState, st.Connected)
	}
	defer func() { _, _ = reg.StopSession(context.Background(), id) }()

	writeCaptureFrame(t, capture, 0x01, "captured-hello\n")
	waitFor(t, "captured output on the screen", func() bool {
		snap, _ := reg.LastSnapshot(ctx, id)
		screen, _ := snap["screen"].(string)
		return strings.Contains(screen, "captured-hello")
	})

	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run: %v", err)
	}
}
