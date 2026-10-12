//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	uptty "github.com/provide-io/provide-uterm/packages/provide-uterm-go/pty"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

var captureSockSeq atomic.Int64

// captureSock returns a short socket path (Unix socket paths are capped near
// 104 bytes, which t.TempDir() can overrun on macOS).
func captureSock(t *testing.T, tag string) string {
	t.Helper()
	p := fmt.Sprintf("/tmp/utcc-%s-%d-%d.sock", tag, os.Getpid(), captureSockSeq.Add(1))
	t.Cleanup(func() { _ = os.Remove(p) })
	return p
}

func sendCapture(t *testing.T, path string, payload string) {
	t.Helper()
	conn, err := net.Dial("unix", path)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.Close() }()
	hdr := make([]byte, 5)
	hdr[0] = uptty.ChannelStdout
	binary.BigEndian.PutUint32(hdr[1:], uint32(len(payload)))
	if _, err := conn.Write(append(hdr, payload...)); err != nil {
		t.Fatalf("write: %v", err)
	}
}

func newCaptureTransport(t *testing.T, cfg map[string]any) *CaptureTransport {
	t.Helper()
	c, err := uptty.NewCaptureConnector("s", "n", cfg)
	if err != nil {
		t.Fatalf("NewCaptureConnector: %v", err)
	}
	return NewCaptureTransport(c)
}

func TestBuildCaptureRejectsBadConfig(t *testing.T) {
	if _, err := Build("s", "n", "pty_capture", map[string]any{}); err == nil ||
		!strings.Contains(err.Error(), "socket_path") {
		t.Fatalf("missing socket_path: err = %v", err)
	}
	if _, err := Build("s", "n", "pty_capture", map[string]any{"socket_path": "/x", "bogus": 1}); err == nil {
		t.Fatal("unknown key accepted")
	}
}

// Through Build, the capture connector is a full Connector: it binds the
// socket on Start, exposes a live Session, renders captured stdout on the
// emulated screen and reports the socket as its upstream.
func TestBuildCaptureConnectorEndToEnd(t *testing.T) {
	sock := captureSock(t, "e2e")
	conn, err := Build("cap", "Cap", "pty_capture", map[string]any{
		"socket_path": sock, "input_mode": "hijack", "cols": 100, "rows": 30,
	})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	ctx := context.Background()
	if err := conn.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer func() { _ = conn.Stop(ctx) }()
	if !conn.IsConnected() || conn.Session() == nil {
		t.Fatal("started capture connector is not connected")
	}
	sendCapture(t, sock, "hello-capture\n")
	deadline := time.Now().Add(3 * time.Second)
	for !strings.Contains(conn.Snapshot().Screen, "hello-capture") {
		if time.Now().After(deadline) {
			t.Fatalf("captured output never reached the screen: %q", conn.Snapshot().Screen)
		}
		time.Sleep(10 * time.Millisecond)
	}
	analysis := conn.Analysis()
	for _, want := range []string{"[pty_capture session analysis", "upstream: unix:" + sock, "input_mode: hijack"} {
		if !strings.Contains(analysis, want) {
			t.Fatalf("analysis missing %q:\n%s", want, analysis)
		}
	}
	if snap := conn.Snapshot(); snap.Cols != 100 || snap.Rows != 30 {
		t.Fatalf("dims = %dx%d, want 100x30", snap.Cols, snap.Rows)
	}
}

// Receive hands out at most maxBytes per call and keeps the rest for the next.
func TestCaptureTransportReceiveSplitsAtMaxBytes(t *testing.T) {
	sock := captureSock(t, "split")
	tr := newCaptureTransport(t, map[string]any{"socket_path": sock})
	ctx := context.Background()
	if err := tr.Connect(ctx, "", 0, transports.ConnectOptions{}); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(ctx) }()
	if !tr.IsConnected() {
		t.Fatal("not connected after Connect")
	}
	sendCapture(t, sock, "abcdef")
	first, err := tr.Receive(ctx, 4, 3*time.Second)
	if err != nil || string(first) != "abcd" {
		t.Fatalf("first = %q, %v; want \"abcd\"", first, err)
	}
	rest, err := tr.Receive(ctx, 4, 0)
	if err != nil || string(rest) != "ef" {
		t.Fatalf("rest = %q, %v; want \"ef\"", rest, err)
	}
}

func TestCaptureTransportReceiveTimesOutEmpty(t *testing.T) {
	tr := newCaptureTransport(t, map[string]any{"socket_path": captureSock(t, "idle")})
	ctx := context.Background()
	if err := tr.Connect(ctx, "", 0, transports.ConnectOptions{}); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(ctx) }()
	data, err := tr.Receive(ctx, 64, 30*time.Millisecond)
	if err != nil || data == nil || len(data) != 0 {
		t.Fatalf("idle Receive = %q, %v; want empty non-nil slice and nil error", data, err)
	}
}

func TestCaptureTransportReceiveAfterDisconnectIsClosed(t *testing.T) {
	tr := newCaptureTransport(t, map[string]any{"socket_path": captureSock(t, "closed")})
	ctx := context.Background()
	if err := tr.Connect(ctx, "", 0, transports.ConnectOptions{}); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	if err := tr.Disconnect(ctx); err != nil {
		t.Fatalf("Disconnect: %v", err)
	}
	if _, err := tr.Receive(ctx, 64, time.Second); !errors.Is(err, transports.ErrConnectionClosed) {
		t.Fatalf("Receive after Disconnect: err = %v, want ErrConnectionClosed", err)
	}
}

func TestCaptureTransportReceiveHonoursContext(t *testing.T) {
	tr := newCaptureTransport(t, map[string]any{"socket_path": captureSock(t, "ctx")})
	if err := tr.Connect(context.Background(), "", 0, transports.ConnectOptions{}); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(context.Background()) }()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := tr.Receive(ctx, 64, time.Second); !errors.Is(err, context.Canceled) {
		t.Fatalf("Receive on cancelled ctx: err = %v, want context.Canceled", err)
	}
}

// Send reaches the captured shell's stdin socket when one is configured.
func TestCaptureTransportSendForwardsToStdinSocket(t *testing.T) {
	stdin := captureSock(t, "stdin")
	ln, err := net.Listen("unix", stdin)
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer func() { _ = ln.Close() }()
	got := make(chan string, 1)
	go func() {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		defer func() { _ = c.Close() }()
		buf := make([]byte, 64)
		n, _ := c.Read(buf)
		got <- string(buf[:n])
	}()

	tr := newCaptureTransport(t, map[string]any{"socket_path": captureSock(t, "out"), "stdin_socket_path": stdin})
	ctx := context.Background()
	if err := tr.Connect(ctx, "", 0, transports.ConnectOptions{}); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(ctx) }()
	if err := tr.Send(ctx, []byte("ls\r")); err != nil {
		t.Fatalf("Send: %v", err)
	}
	select {
	case s := <-got:
		if s != "ls\r" {
			t.Fatalf("stdin got %q, want \"ls\\r\"", s)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("input never reached the stdin socket")
	}
}

func sendCaptureGeometry(t *testing.T, path string, cols, rows uint16) {
	t.Helper()
	conn, err := net.Dial("unix", path)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.Close() }()
	frame := []byte{uptty.ChannelGeometry, 0, 0, 0, 4, 0, 0, 0, 0}
	binary.BigEndian.PutUint16(frame[5:], cols)
	binary.BigEndian.PutUint16(frame[7:], rows)
	if _, err := conn.Write(frame); err != nil {
		t.Fatalf("write: %v", err)
	}
}

// A geometry frame on the capture wire resizes the session the runtime serves:
// its snapshot reports the new size, the screen is redrawn at it from what was
// drawn since the last clear, and the session's watchers hear about it.
func TestCaptureConnectorFollowsGeometry(t *testing.T) {
	sock := captureSock(t, "geo")
	conn, err := Build("cap", "Cap", "pty_capture", map[string]any{"socket_path": sock, "cols": 80, "rows": 24})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	ctx := context.Background()
	if err := conn.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer func() { _ = conn.Stop(ctx) }()
	resized := make(chan map[string]any, 4)
	conn.Session().AddWatch(func(state map[string]any, raw []byte) {
		if raw == nil {
			resized <- state
		}
	})

	sendCapture(t, sock, "\x1b[2J\x1b[H"+strings.Repeat("x", 90))
	deadline := time.Now().Add(3 * time.Second)
	for !strings.Contains(conn.Snapshot().Screen, "xxxxxxxxxx") && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	sendCaptureGeometry(t, sock, 100, 40)

	select {
	case state := <-resized:
		if state["cols"] != 100 || state["rows"] != 40 {
			t.Fatalf("resize note = %v", state)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("the session never heard the geometry frame")
	}
	snap := conn.Snapshot()
	if snap.Cols != 100 || snap.Rows != 40 {
		t.Fatalf("snapshot size = %dx%d, want 100x40", snap.Cols, snap.Rows)
	}
	if first := strings.SplitN(snap.Screen, "\n", 2)[0]; strings.TrimRight(first, " ") != strings.Repeat("x", 90) {
		t.Fatalf("first row = %q, want the 90 x's redrawn on one row", first)
	}
}

// A restarted connector comes back at the size it was last told, not the one
// it was created with: the capture side remembers the geometry, and a session
// built at the old size would never be told again.
func TestCaptureConnectorRestartKeepsTheGeometry(t *testing.T) {
	sock := captureSock(t, "geor")
	conn, err := Build("cap", "Cap", "pty_capture", map[string]any{"socket_path": sock})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	ctx := context.Background()
	if err := conn.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	sendCaptureGeometry(t, sock, 120, 30)
	deadline := time.Now().Add(3 * time.Second)
	for conn.Snapshot().Cols != 120 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if err := conn.Stop(ctx); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if err := conn.Start(ctx); err != nil {
		t.Fatalf("Start again: %v", err)
	}
	defer func() { _ = conn.Stop(ctx) }()
	if snap := conn.Snapshot(); snap.Cols != 120 || snap.Rows != 30 {
		t.Fatalf("restarted at %dx%d, want 120x30", snap.Cols, snap.Rows)
	}
}

// Output that arrives in the same poll as the geometry, before it, reaches the
// emulator before the resize does: the redraw replaces the screen it drew, so
// it is not drawn twice and not lost.
func TestCaptureTransportAppliesTheResizeAfterThePendingOutput(t *testing.T) {
	sock := captureSock(t, "geot")
	tr := newCaptureTransport(t, map[string]any{"socket_path": sock})
	var got [][2]int
	var redrawn []byte
	tr.OnResize(func(cols, rows int, redraw []byte) {
		got = append(got, [2]int{cols, rows})
		redrawn = redraw
	})
	ctx := context.Background()
	if err := tr.Connect(ctx, "", 0, transports.ConnectOptions{}); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(ctx) }()

	conn, err := net.Dial("unix", sock)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	out := "old\x1b[2Jnew"
	hdr := []byte{uptty.ChannelStdout, 0, 0, 0, 0}
	binary.BigEndian.PutUint32(hdr[1:], uint32(len(out)))
	geo := []byte{uptty.ChannelGeometry, 0, 0, 0, 4, 0, 100, 0, 40}
	_, _ = conn.Write(append(append(hdr, out...), geo...))
	_ = conn.Close()

	var data []byte
	deadline := time.Now().Add(3 * time.Second)
	for len(got) == 0 && time.Now().Before(deadline) {
		chunk, err := tr.Receive(ctx, 4, 50*time.Millisecond)
		if err != nil {
			t.Fatalf("Receive: %v", err)
		}
		if len(chunk) > 0 && len(got) > 0 {
			t.Fatal("output was handed over after the resize it preceded")
		}
		data = append(data, chunk...)
	}
	if string(data) != out {
		t.Fatalf("output = %q, want all of %q before the resize", data, out)
	}
	if len(got) != 1 || got[0] != [2]int{100, 40} {
		t.Fatalf("resizes = %v", got)
	}
	if string(redrawn) != "\x1b[2Jnew" {
		t.Fatalf("redraw = %q, want the output since the last full clear", redrawn)
	}
}

// The redraw is bounded: a program that never clears again cannot grow it past
// captureReplayMax; it keeps the newest output.
func TestCaptureTransportBoundsTheRedraw(t *testing.T) {
	tr := &CaptureTransport{}
	tr.noteOutput([]byte("\x1b[2Jhead"))
	tr.noteOutput(bytes.Repeat([]byte("y"), captureReplayMax))
	if len(tr.sinceClear) != captureReplayMax {
		t.Fatalf("redraw is %d bytes, want %d", len(tr.sinceClear), captureReplayMax)
	}
	if !bytes.HasSuffix(tr.sinceClear, []byte("yyy")) || bytes.Contains(tr.sinceClear, []byte("head")) {
		t.Fatal("the redraw must keep the newest output")
	}
}
