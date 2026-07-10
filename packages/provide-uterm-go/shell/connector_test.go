//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"context"
	"strings"
	"testing"
	"time"
)

// newTestConnector builds a connector with a no-op poll sleep so tests do not
// block on the idle back-off.
func newTestConnector(sessionID string, cfg ConnectorConfig) *UshellConnector {
	c := NewUshellConnector(sessionID, cfg)
	c.pollSleep = func(time.Duration) {}
	return c
}

func framesData(frames []Frame) string {
	parts := make([]string, 0, len(frames))
	for _, f := range frames {
		if d, ok := f["data"].(string); ok {
			parts = append(parts, d)
		}
	}
	return strings.Join(parts, " ")
}

func TestConnectorLifecycle(t *testing.T) {
	c := newTestConnector("test-session", ConnectorConfig{})
	if c.IsConnected() {
		t.Fatal("should start disconnected")
	}
	c.Start()
	if !c.IsConnected() {
		t.Fatal("should be connected after Start")
	}
	c.Stop()
	if c.IsConnected() {
		t.Fatal("should be disconnected after Stop")
	}
}

func TestConnectorDefaults(t *testing.T) {
	c := newTestConnector("", ConnectorConfig{})
	if c.sessionID != "" || c.displayName != "" {
		t.Fatalf("defaults = %q / %q", c.sessionID, c.displayName)
	}
	if c.welcomed || c.flowPaused {
		t.Fatal("welcomed/flowPaused should start false")
	}
	if got := NewUshellConnector("my-id", ConnectorConfig{}).displayName; got != "my-id" {
		t.Fatalf("display default = %q", got)
	}
	if got := NewUshellConnector("my-id", ConnectorConfig{DisplayName: "Pretty Name"}).displayName; got != "Pretty Name" {
		t.Fatalf("display override = %q", got)
	}
}

func TestConnectorConfigParamAccepted(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{ExtraCtx: map[string]any{"unused": true}})
	c.Start()
	if !c.IsConnected() {
		t.Fatal("config param should not break connect")
	}
}

func TestConnectorPollNotConnected(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	if got := c.PollMessages(); len(got) != 0 {
		t.Fatalf("poll = %v, want empty", got)
	}
}

func TestConnectorPollWelcome(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	frames := c.PollMessages()
	if len(frames) != 2 {
		t.Fatalf("welcome frames = %d, want 2", len(frames))
	}
	if frames[0]["type"] != "worker_hello" || frames[0]["input_mode"] != "open" {
		t.Fatalf("frame0 = %v", frames[0])
	}
	if frames[1]["type"] != "term" {
		t.Fatalf("frame1 type = %v", frames[1]["type"])
	}
	data := frames[1]["data"].(string)
	if !strings.Contains(data, Banner) || !strings.Contains(data, Prompt) {
		t.Fatalf("frame1 data = %q", data)
	}
}

func TestConnectorFlowPause(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	c.PollMessages() // consume welcome
	c.pending = []Frame{{"type": "term", "data": "x"}}
	if got := c.HandleControl("flow_pause"); len(got) != 0 {
		t.Fatalf("flow_pause returned %v", got)
	}
	if !c.flowPaused {
		t.Fatal("flowPaused not set")
	}
	if got := c.PollMessages(); len(got) != 0 {
		t.Fatalf("poll while paused = %v, want withheld", got)
	}
	if got := c.HandleControl("flow_resume"); len(got) != 0 {
		t.Fatalf("flow_resume returned %v", got)
	}
	got := c.PollMessages()
	if len(got) != 1 || got[0]["data"] != "x" {
		t.Fatalf("poll after resume = %v", got)
	}
}

func TestConnectorHijackActionsNoop(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	for _, a := range []string{"pause", "resume", "step"} {
		if got := c.HandleControl(a); len(got) != 0 {
			t.Fatalf("%s returned %v", a, got)
		}
	}
	if c.flowPaused {
		t.Fatal("hijack action set flowPaused")
	}
}

func TestConnectorSnapshotRequest(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	out := c.HandleControl("snapshot_request")
	if len(out) != 1 || out[0]["type"] != "snapshot" {
		t.Fatalf("snapshot_request = %v", out)
	}
}

func TestConnectorHandleInputEcho(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	frames := c.HandleInput(context.Background(), "abc")
	if len(frames) != 1 || frames[0]["data"] != "abc" {
		t.Fatalf("= %v", frames)
	}
}

func TestConnectorHandleInputDispatch(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	frames := c.HandleInput(context.Background(), "help\r")
	if len(frames) < 2 {
		t.Fatalf("frames = %d, want >= 2", len(frames))
	}
	if !strings.Contains(framesData(frames), "ushell commands") {
		t.Fatalf("data = %q", framesData(frames))
	}
}

func TestConnectorHandleInputNoEcho(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	if got := c.HandleInput(context.Background(), "\x01"); len(got) != 0 {
		t.Fatalf("= %v", got)
	}
}

func TestConnectorHandleInputCtrlC(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	frames := c.HandleInput(context.Background(), "\x03")
	if !strings.Contains(framesData(frames), "^C") {
		t.Fatalf("= %q", framesData(frames))
	}
}

func TestConnectorSnapshotStructure(t *testing.T) {
	c := newTestConnector("my-session", ConnectorConfig{})
	c.Start()
	snap := c.GetSnapshot()
	if snap["type"] != "snapshot" {
		t.Fatalf("type = %v", snap["type"])
	}
	if !strings.Contains(snap["screen"].(string), "my-session") {
		t.Fatalf("screen = %q", snap["screen"])
	}
	if snap["cols"] != 80 || snap["rows"] != 24 {
		t.Fatalf("cols/rows = %v/%v", snap["cols"], snap["rows"])
	}
	if snap["cursor_at_end"] != true || snap["has_trailing_space"] != false {
		t.Fatalf("cursor flags = %v/%v", snap["cursor_at_end"], snap["has_trailing_space"])
	}
	if snap["prompt_detected"].(map[string]string)["prompt_id"] != "ushell_prompt" {
		t.Fatalf("prompt_detected = %v", snap["prompt_detected"])
	}
	cur := snap["cursor"].(map[string]int)
	if cur["y"] != 1 {
		t.Fatalf("cursor y = %d", cur["y"])
	}
}

func TestConnectorSnapshotCursorUpdates(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	c.HandleInput(context.Background(), "hello")
	snap := c.GetSnapshot()
	cur := snap["cursor"].(map[string]int)
	want := len([]rune(Prompt)) + 5
	if cur["x"] != want {
		t.Fatalf("cursor x = %d, want %d", cur["x"], want)
	}
}

func TestConnectorSnapshotHashDiffers(t *testing.T) {
	c1 := newTestConnector("aaa-session", ConnectorConfig{})
	c2 := newTestConnector("bbb-session", ConnectorConfig{})
	c1.Start()
	c2.Start()
	if c1.GetSnapshot()["screen_hash"] == c2.GetSnapshot()["screen_hash"] {
		t.Fatal("distinct screens produced equal hashes")
	}
}

func TestConnectorAnalysis(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{ExtraCtx: map[string]any{"k": 1}})
	c.Start()
	analysis := c.GetAnalysis()
	lines := strings.Split(analysis, "\n")
	if !strings.HasPrefix(lines[0], "[ushell analysis") {
		t.Fatalf("line0 = %q", lines[0])
	}
	if lines[1] != "connected: true" {
		t.Fatalf("line1 = %q", lines[1])
	}
	if !strings.HasPrefix(lines[2], "current_line:") || !strings.HasPrefix(lines[3], "context_names:") {
		t.Fatalf("lines = %v", lines)
	}
	if !strings.Contains(lines[3], "k") {
		t.Fatalf("context_names = %q", lines[3])
	}
}

func TestConnectorAnalysisNotConnected(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	if !strings.Contains(c.GetAnalysis(), "connected: false") {
		t.Fatalf("= %q", c.GetAnalysis())
	}
}

func TestConnectorClear(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	c.HandleInput(context.Background(), "some text")
	frames := c.Clear()
	if len(frames) != 1 || !strings.Contains(frames[0]["data"].(string), "\x1b[2J") {
		t.Fatalf("= %v", frames)
	}
	if c.buf.CurrentLine() != "" {
		t.Fatal("buffer not cleared")
	}
}

func TestConnectorSetMode(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	for _, mode := range []string{"hijack", "open"} {
		frames := c.SetMode(mode)
		if len(frames) != 1 || frames[0]["type"] != "worker_hello" || frames[0]["input_mode"] != mode {
			t.Fatalf("set_mode(%q) = %v", mode, frames)
		}
	}
}

func TestConnectorExtraCtxInEnv(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{ExtraCtx: map[string]any{"MY_KEY": "hello"}})
	c.Start()
	frames := c.HandleInput(context.Background(), "env\r")
	if !strings.Contains(framesData(frames), "MY_KEY") {
		t.Fatalf("= %q", framesData(frames))
	}
}

func TestConnectorPollReturnsPending(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	c.PollMessages() // consume welcome
	c.pending = append(c.pending, Frame{"type": "term", "data": "test-pending"})
	frames := c.PollMessages()
	if len(frames) != 1 || frames[0]["data"] != "test-pending" {
		t.Fatalf("= %v", frames)
	}
	if got := c.PollMessages(); len(got) != 0 {
		t.Fatalf("pending not cleared: %v", got)
	}
}

func TestConnectorPollSleepDuration(t *testing.T) {
	c := NewUshellConnector("s1", ConnectorConfig{})
	var recorded time.Duration
	calls := 0
	c.pollSleep = func(d time.Duration) { recorded = d; calls++ }
	c.Start()
	c.PollMessages() // welcome: no sleep
	c.PollMessages() // idle: sleep
	if calls != 1 || recorded != defaultPollDelay {
		t.Fatalf("sleep calls=%d recorded=%v", calls, recorded)
	}
}

func TestConnectorAnimationStreams(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	c.PollMessages() // consume welcome
	c.startAnimation(AnimatedResult{Frames: []string{"frame1\r\n", "frame2\r\n"}, FPS: 1000, Loop: false})

	waitAnim(t, c)

	// Collect all pending frames.
	frames := c.PollMessages()
	data := framesData(frames)
	if !strings.Contains(data, "frame1") || !strings.Contains(data, "frame2") {
		t.Fatalf("animation frames = %q", data)
	}
	if !strings.Contains(data, Prompt) {
		t.Fatalf("missing trailing prompt: %q", data)
	}
}

func TestConnectorAnimationZeroFPS(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	c.PollMessages() // consume welcome so later polls return pending
	c.startAnimation(AnimatedResult{Frames: []string{"x\r\n"}, FPS: 0, Loop: false})
	waitAnim(t, c)
	if !strings.Contains(framesData(c.PollMessages()), "x") {
		t.Fatal("zero-fps animation produced no frame")
	}
}

func TestConnectorStopCancelsAnimation(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	c.startAnimation(AnimatedResult{Frames: []string{"f1\r\n", "f2\r\n"}, FPS: 1000, Loop: true})
	done := c.waitAnimDone()
	time.Sleep(10 * time.Millisecond)
	c.Stop()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("looping animation not cancelled by Stop")
	}
}

func TestConnectorReplacesAnimation(t *testing.T) {
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	c.startAnimation(AnimatedResult{Frames: []string{"a\r\n"}, FPS: 50, Loop: true})
	done1 := c.waitAnimDone()
	c.startAnimation(AnimatedResult{Frames: []string{"b\r\n"}, FPS: 1000, Loop: false})
	select {
	case <-done1:
	case <-time.After(2 * time.Second):
		t.Fatal("first animation not cancelled on replace")
	}
	waitAnim(t, c)
}

func TestConnectorHandleInputAnimated(t *testing.T) {
	// Drive the AnimatedResult branch of HandleInput via a real cast command.
	p := writeTemp(t, makeCastText(2, [][2]any{{0.0, "castframe\r\n"}, {0.05, "more\r\n"}}))
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	c.PollMessages()
	frames := c.HandleInput(context.Background(), "cast --fps 1000 file://"+p+"\r")
	// The echo frame is returned inline; animation streams via pending.
	if !strings.Contains(framesData(frames), "\r\n") {
		t.Fatalf("expected echo frame, got %q", framesData(frames))
	}
	waitAnim(t, c)
	if !strings.Contains(framesData(c.PollMessages()), "castframe") {
		t.Fatal("cast animation frames not delivered")
	}
}

func TestConnectorEmptyFramesLoopCancel(t *testing.T) {
	// An empty-frame looping animation spins the outer loop until cancelled,
	// exercising the between-iteration ctx.Done() branch.
	c := newTestConnector("s1", ConnectorConfig{})
	c.Start()
	c.startAnimation(AnimatedResult{Frames: nil, FPS: 1000, Loop: true})
	done := c.waitAnimDone()
	c.Stop()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("empty-frame looping animation not cancelled")
	}
}

// waitAnim blocks until the connector's current animation goroutine finishes.
func waitAnim(t *testing.T, c *UshellConnector) {
	t.Helper()
	done := c.waitAnimDone()
	if done == nil {
		t.Fatal("no animation running")
	}
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("animation did not finish")
	}
}
