//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
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
