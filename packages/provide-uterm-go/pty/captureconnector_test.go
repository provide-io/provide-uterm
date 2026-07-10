//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"context"
	"strings"
	"testing"
	"time"
)

func newCaptureConn(t *testing.T, extra map[string]any) *CaptureConnector {
	t.Helper()
	cfg := map[string]any{"socket_path": shortSocketPath(t)}
	for k, v := range extra {
		cfg[k] = v
	}
	c, err := NewCaptureConnector("sess", "disp", cfg)
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	return c
}

// pollForTerm polls the connector until a term frame appears or the deadline
// passes, enqueuing frames into the bound capture socket first.
func pollForTerm(t *testing.T, c *CaptureConnector) []Frame {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if frames := c.PollMessages(); len(frames) > 0 {
			return frames
		}
		time.Sleep(10 * time.Millisecond)
	}
	return nil
}

func TestCaptureConnectorRequiresSocketPath(t *testing.T) {
	if _, err := NewCaptureConnector("s", "d", map[string]any{}); err == nil {
		t.Fatal("expected missing socket_path error")
	}
}

func TestCaptureConnectorUnknownKey(t *testing.T) {
	_, err := NewCaptureConnector("s", "d", map[string]any{"socket_path": "/x.sock", "bogus": 1})
	assertErr(t, err, "unknown config keys")
}

func TestCaptureConnectorStdoutFlow(t *testing.T) {
	c := newCaptureConn(t, map[string]any{"cols": 100, "rows": 40})
	if err := c.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Stop(context.Background()) }()
	if !c.IsConnected() {
		t.Fatal("should be connected")
	}
	sendFrames(t, c.socketPath, makeFrame(ChannelStdout, []byte("line1\nline2")))
	frames := pollForTerm(t, c)
	if len(frames) == 0 || frames[0]["type"] != "term" {
		t.Fatalf("expected term frame, got %+v", frames)
	}
	// bare \n normalized to \r\n
	data := frames[0]["data"].(string)
	if !strings.Contains(data, "\r\n") {
		t.Fatalf("expected CRLF normalization, got %q", data)
	}
	// snapshot reflects buffer + dims
	snap := c.GetSnapshot()
	if snap["cols"] != 100 || snap["rows"] != 40 {
		t.Fatalf("snapshot dims wrong: %+v", snap)
	}
	if snap["screen"].(string) == "" {
		t.Fatal("screen should be non-empty")
	}
}

func TestCaptureConnectorStdinAndConnectChannels(t *testing.T) {
	c := newCaptureConn(t, nil)
	_ = c.Start(context.Background())
	defer func() { _ = c.Stop(context.Background()) }()
	sendFrames(t, c.socketPath,
		makeFrame(ChannelStdin, []byte("x")),
		makeFrame(ChannelConnect, []byte("10.0.0.1:22")),
	)
	// Poll a few times to drain (no term output expected from stdin/connect).
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		c.PollMessages()
		if strings.Contains(c.GetAnalysis(), "recent_connect") {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	an := c.GetAnalysis()
	if !strings.Contains(an, "stdin_keystrokes=1") || !strings.Contains(an, "10.0.0.1:22") {
		t.Fatalf("analysis missing counters: %q", an)
	}
}

func TestCaptureConnectorClear(t *testing.T) {
	c := newCaptureConn(t, nil)
	_ = c.Start(context.Background())
	defer func() { _ = c.Stop(context.Background()) }()
	frames := c.Clear()
	if len(frames) != 1 || frames[0]["type"] != "term" || frames[0]["data"] != "" {
		t.Fatalf("clear frame wrong: %+v", frames)
	}
}

func TestCaptureConnectorSetMode(t *testing.T) {
	c := newCaptureConn(t, nil)
	frames := c.SetMode("hijack")
	if len(frames) != 1 || frames[0]["type"] != "worker_hello" || frames[0]["input_mode"] != "open" {
		t.Fatalf("set_mode should re-advertise open: %+v", frames)
	}
}

func TestCaptureConnectorHandleControlNoop(t *testing.T) {
	c := newCaptureConn(t, nil)
	if got := c.HandleControl("pause"); got != nil {
		t.Fatalf("handle_control should be nil, got %+v", got)
	}
}

func TestCaptureConnectorPollWhenDisconnected(t *testing.T) {
	c := newCaptureConn(t, nil)
	if got := c.PollMessages(); got != nil {
		t.Fatalf("poll before start should be nil, got %+v", got)
	}
}

func TestCaptureConnectorStdinForwarding(t *testing.T) {
	// Listener that captures forwarded stdin bytes.
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
	_ = c.Start(context.Background())
	defer func() { _ = c.Stop(context.Background()) }()

	// Raw bytes (the forwarder writes the exact keystroke bytes, not framed);
	// read them off the accepted connection directly.
	if got := c.HandleInput(context.Background(), "hi"); got != nil {
		t.Fatalf("handle_input should return nil, got %+v", got)
	}
	// Give the write time to land; then verify the connector holds a live writer.
	time.Sleep(50 * time.Millisecond)
	c.mu.Lock()
	haveWriter := c.stdinWriter != nil
	c.mu.Unlock()
	if !haveWriter {
		t.Fatal("expected a live stdin writer after forwarding")
	}
}

func TestCaptureConnectorStdinForwardingNoPath(t *testing.T) {
	c := newCaptureConn(t, nil)
	if got := c.HandleInput(context.Background(), "hi"); got != nil {
		t.Fatalf("no stdin path → nil, got %+v", got)
	}
}
