//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"context"
	"io"
	"strings"
	"testing"
	"time"
)

func makeConn(t *testing.T, command string, args []string, extra map[string]any) *PTYConnector {
	t.Helper()
	cfg := map[string]any{"command": command}
	if args != nil {
		anyArgs := make([]any, len(args))
		for i, a := range args {
			anyArgs[i] = a
		}
		cfg["args"] = anyArgs
	}
	for k, v := range extra {
		cfg[k] = v
	}
	c, err := NewPTYConnector("sess", "disp", cfg)
	if err != nil {
		t.Fatalf("new connector: %v", err)
	}
	return c
}

// waitForScreen polls the snapshot buffer until it contains substr or times out.
func waitForScreen(t *testing.T, c *PTYConnector, substr string) string {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		c.PollMessages() // drive the dirty flag; harmless
		screen, _ := c.GetSnapshot()["screen"].(string)
		if strings.Contains(screen, substr) {
			return screen
		}
		time.Sleep(15 * time.Millisecond)
	}
	screen, _ := c.GetSnapshot()["screen"].(string)
	t.Fatalf("screen never contained %q; got %q", substr, screen)
	return ""
}

func TestConnectorEchoHi(t *testing.T) {
	c := makeConn(t, "/bin/echo", []string{"hi"}, nil)
	if err := c.Start(context.Background()); err != nil {
		t.Fatalf("start: %v", err)
	}
	defer func() { _ = c.Stop(context.Background()) }()
	waitForScreen(t, c, "hi")
}

func TestConnectorCatHandleInput(t *testing.T) {
	c := makeConn(t, "/bin/cat", nil, nil)
	if err := c.Start(context.Background()); err != nil {
		t.Fatalf("start: %v", err)
	}
	defer func() { _ = c.Stop(context.Background()) }()
	if !c.IsConnected() {
		t.Fatal("should be connected")
	}
	msgs := c.HandleInput(context.Background(), "hello\n")
	if !anyType(msgs, "snapshot") {
		t.Fatalf("handle_input should return a snapshot: %+v", msgs)
	}
	waitForScreen(t, c, "hello")
}

func TestConnectorSnapshotKeys(t *testing.T) {
	c := makeConn(t, "/bin/echo", nil, map[string]any{"cols": 132, "rows": 50})
	_ = c.Start(context.Background())
	defer func() { _ = c.Stop(context.Background()) }()
	snap := c.GetSnapshot()
	if snap["type"] != "snapshot" || snap["cols"] != 132 || snap["rows"] != 50 {
		t.Fatalf("snapshot fields: %+v", snap)
	}
	if snap["cursor_at_end"] != true || snap["has_trailing_space"] != false || snap["prompt_detected"] != false {
		t.Fatalf("snapshot static fields: %+v", snap)
	}
	cur, ok := snap["cursor"].(map[string]int)
	if !ok || cur["row"] != 0 || cur["col"] != 0 {
		t.Fatalf("cursor: %+v", snap["cursor"])
	}
	screen := snap["screen"].(string)
	if snap["screen_hash"] != md5Hex(screen) {
		t.Fatalf("screen_hash mismatch")
	}
}

func TestConnectorSetMode(t *testing.T) {
	c := makeConn(t, "/bin/cat", nil, nil)
	_ = c.Start(context.Background())
	defer func() { _ = c.Stop(context.Background()) }()
	msgs, err := c.SetMode("hijack")
	if err != nil {
		t.Fatal(err)
	}
	if !anyType(msgs, "worker_hello") || !anyType(msgs, "snapshot") {
		t.Fatalf("set_mode frames: %+v", msgs)
	}
	for _, m := range msgs {
		if m["type"] == "worker_hello" && m["input_mode"] != "hijack" {
			t.Fatalf("input_mode = %v", m["input_mode"])
		}
	}
	if _, err := c.SetMode("superuser"); err == nil {
		t.Fatal("expected invalid mode error")
	}
}

func TestConnectorClear(t *testing.T) {
	c := makeConn(t, "/bin/cat", nil, nil)
	_ = c.Start(context.Background())
	defer func() { _ = c.Stop(context.Background()) }()
	c.feed([]byte("garbage"))
	msgs := c.Clear()
	if !anyType(msgs, "snapshot") {
		t.Fatalf("clear frames: %+v", msgs)
	}
	if c.GetSnapshot()["screen"] != "" {
		t.Fatal("screen should be empty after clear")
	}
}

func TestConnectorPauseResume(t *testing.T) {
	c := makeConn(t, "/bin/cat", nil, nil)
	_ = c.Start(context.Background())
	defer func() { _ = c.Stop(context.Background()) }()
	if !anyType(c.HandleControl("pause"), "snapshot") {
		t.Fatal("pause should return snapshot")
	}
	// Paused with data → poll returns nil.
	c.feed([]byte("data"))
	if got := c.PollMessages(); got != nil {
		t.Fatalf("paused poll should be nil, got %+v", got)
	}
	if !anyType(c.HandleControl("resume"), "snapshot") {
		t.Fatal("resume should return snapshot")
	}
	// step also resumes
	c.HandleControl("pause")
	c.HandleControl("step")
	if got := c.PollMessages(); got == nil {
		t.Fatal("after step, poll with data should return snapshot")
	}
}

func TestConnectorFlowPause(t *testing.T) {
	c := makeConn(t, "/bin/cat", nil, nil)
	_ = c.Start(context.Background())
	defer func() { _ = c.Stop(context.Background()) }()
	if got := c.HandleControl("flow_pause"); got != nil {
		t.Fatalf("flow_pause should emit nothing, got %+v", got)
	}
	c.feed([]byte("data"))
	if got := c.PollMessages(); got != nil {
		t.Fatalf("flow-paused poll should be nil, got %+v", got)
	}
	if got := c.HandleControl("flow_resume"); got != nil {
		t.Fatalf("flow_resume should emit nothing, got %+v", got)
	}
	if got := c.PollMessages(); got == nil {
		t.Fatal("after flow_resume, poll with data should return snapshot")
	}
}

func TestConnectorHandleControlUnknown(t *testing.T) {
	c := makeConn(t, "/bin/cat", nil, nil)
	_ = c.Start(context.Background())
	defer func() { _ = c.Stop(context.Background()) }()
	if !anyType(c.HandleControl("bogus"), "snapshot") {
		t.Fatal("unknown action should return snapshot")
	}
}

func TestConnectorAnalysis(t *testing.T) {
	c := makeConn(t, "/bin/echo", nil, nil)
	_ = c.Start(context.Background())
	defer func() { _ = c.Stop(context.Background()) }()
	if !strings.Contains(c.GetAnalysis(), "/bin/echo") {
		t.Fatalf("analysis missing command: %s", c.GetAnalysis())
	}
}

func TestConnectorReadLoopEOFDisconnects(t *testing.T) {
	c := makeConn(t, "/bin/cat", nil, nil)
	c.connected = true
	c.readDone = make(chan struct{})
	pr, pw := io.Pipe()
	go c.readLoop(pr)
	_, _ = pw.Write([]byte("hello"))
	_ = pw.Close() // EOF
	select {
	case <-c.readDone:
	case <-time.After(2 * time.Second):
		t.Fatal("readLoop did not exit on EOF")
	}
	c.mu.Lock()
	connected := c.connected
	buf := c.buffer
	c.mu.Unlock()
	if connected {
		t.Fatal("EOF should mark disconnected")
	}
	if buf != "hello" {
		t.Fatalf("buffer = %q", buf)
	}
}

func TestConnectorFeedBufferCap(t *testing.T) {
	c := makeConn(t, "/bin/cat", nil, nil)
	c.buffer = strings.Repeat("a", 32764)
	c.feed([]byte(strings.Repeat("b", 10)))
	if len(c.buffer) != bufferCap {
		t.Fatalf("buffer len = %d, want %d", len(c.buffer), bufferCap)
	}
}

func TestConnectorHandleInputWhenPausedNoWrite(t *testing.T) {
	c := makeConn(t, "/bin/cat", nil, nil)
	_ = c.Start(context.Background())
	defer func() { _ = c.Stop(context.Background()) }()
	c.HandleControl("pause")
	// Should not raise / should still return a snapshot.
	if !anyType(c.HandleInput(context.Background(), "ignored\n"), "snapshot") {
		t.Fatal("handle_input while paused should still return a snapshot")
	}
}

func anyType(frames []Frame, typ string) bool {
	for _, f := range frames {
		if f["type"] == typ {
			return true
		}
	}
	return false
}
