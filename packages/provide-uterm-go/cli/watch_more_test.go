//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/coder/websocket"
)

// runWatchProgram builds a headless bubbletea program over the watch model with
// a non-TTY input (immediate EOF) and a discarded renderer, runs it, and returns
// a channel that yields the final model once the program exits.
func runWatchProgram(t *testing.T, layout string) (*tea.Program, chan tea.Model) {
	t.Helper()
	p := tea.NewProgram(
		newWatchModel("tun", layout),
		tea.WithInput(strings.NewReader("")),
		tea.WithOutput(io.Discard),
	)
	modelCh := make(chan tea.Model, 1)
	go func() {
		m, _ := p.Run()
		modelCh <- m
	}()
	return p, modelCh
}

// TestWatchWSLoopDeliversFrames drives watchWSLoop against a live WebSocket that
// pushes a non-text frame (skipped) followed by an HTTP-channel text frame, then
// closes — exercising the connect/binary-continue/text-frame/read-error paths.
func TestWatchWSLoopDeliversFrames(t *testing.T) {
	frame := makeChannelFrame(map[string]any{
		"_channel": "http", "type": "http_req", "id": "r1", "method": "GET", "url": "/live",
	})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer c.CloseNow() //nolint:errcheck // test cleanup
		// A binary message must be ignored by the parser.
		_ = c.Write(r.Context(), websocket.MessageBinary, []byte{0x01, 0x02})
		_ = c.Write(r.Context(), websocket.MessageText, []byte(frame))
		// Close so the reader's Read returns an error and the loop exits.
		_ = c.Close(websocket.StatusNormalClosure, "done")
	}))
	defer srv.Close()

	wsURL := "ws" + strings.TrimPrefix(srv.URL, "http") + "/ws"
	p, modelCh := runWatchProgram(t, "horizontal")
	watchWSLoop(context.Background(), wsURL, http.Header{}, p)
	p.Quit()

	select {
	case m := <-modelCh:
		wm := m.(watchModel)
		if len(wm.exchanges) != 1 || wm.exchanges[0].method != "GET" {
			t.Fatalf("expected one GET exchange, got %+v", wm.exchanges)
		}
		if wm.connected {
			t.Errorf("connection should be marked closed after server hangup")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("watch program did not exit")
	}
}

// TestWatchWSLoopDialFailure covers the dial-error branch: an unreachable URL
// must mark the connection down and return without blocking.
func TestWatchWSLoopDialFailure(t *testing.T) {
	// Reserve then release a port so the dial is guaranteed to be refused.
	port := freePort(t)
	wsURL := "ws://127.0.0.1:" + strconv.Itoa(port) + "/ws"

	p, modelCh := runWatchProgram(t, "horizontal")
	watchWSLoop(context.Background(), wsURL, http.Header{}, p)
	p.Quit()

	select {
	case m := <-modelCh:
		if wm := m.(watchModel); wm.connected || len(wm.exchanges) != 0 {
			t.Errorf("dial failure should leave model unconnected/empty, got %+v", wm)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("watch program did not exit after dial failure")
	}
}

// TestRunWatchViaCommand exercises the full `watch` command wiring end to end:
// the RunE closure, runWatch, watchModel.Init, and the WS reader goroutine. The
// program is torn down by cancelling the command context. stdin/stdout are
// pointed at /dev/null so the alt-screen renderer neither reads a TTY nor
// pollutes the test output.
func TestRunWatchViaCommand(t *testing.T) {
	frame := makeChannelFrame(map[string]any{
		"_channel": "http", "type": "http_req", "id": "c1", "method": "POST", "url": "/x",
	})
	connected := make(chan struct{}, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer c.CloseNow() //nolint:errcheck // test cleanup
		select {
		case connected <- struct{}{}:
		default:
		}
		_ = c.Write(r.Context(), websocket.MessageText, []byte(frame))
		// Block until the client goes away (command context cancelled).
		for {
			if _, _, rerr := c.Read(r.Context()); rerr != nil {
				return
			}
		}
	}))
	defer srv.Close()

	devR, err := os.Open(os.DevNull)
	if err != nil {
		t.Fatalf("open devnull read: %v", err)
	}
	devW, err := os.OpenFile(os.DevNull, os.O_WRONLY, 0)
	if err != nil {
		t.Fatalf("open devnull write: %v", err)
	}
	oldIn, oldOut := os.Stdin, os.Stdout
	os.Stdin, os.Stdout = devR, devW
	defer func() {
		os.Stdin, os.Stdout = oldIn, oldOut
		_ = devR.Close()
		_ = devW.Close()
	}()

	ctx, cancel := context.WithCancel(context.Background())
	root := NewRootCmd()
	root.SetArgs([]string{"watch", "tunnel-9", "--server", srv.URL})
	root.SetOut(io.Discard)
	root.SetErr(io.Discard)

	done := make(chan error, 1)
	go func() { done <- root.ExecuteContext(ctx) }()

	// Wait for the WS reader to attach before tearing the program down.
	select {
	case <-connected:
	case <-time.After(5 * time.Second):
		cancel()
		t.Fatal("watch command never opened the upstream WebSocket")
	}
	cancel()

	select {
	case <-done:
		// A cancelled bubbletea context returns either nil or ErrProgramKilled;
		// both are acceptable — the point is that the command unwinds cleanly.
	case <-time.After(5 * time.Second):
		t.Fatal("watch command did not exit after context cancel")
	}
}

// TestWatchModelSmallBranches covers the pure model branches not hit elsewhere:
// window resize, the cycleLayout fallback for an out-of-list mode, the
// cycleMethod cursor reset, and the handleFrame response-matching skip.
func TestWatchModelSmallBranches(t *testing.T) {
	// WindowSizeMsg updates the geometry.
	m := newWatchModel("t", "horizontal")
	nm, _ := m.Update(tea.WindowSizeMsg{Width: 120, Height: 40})
	m = nm.(watchModel)
	if m.width != 120 || m.height != 40 {
		t.Fatalf("resize not applied: %dx%d", m.width, m.height)
	}

	// cycleLayout falls back to horizontal for an unrecognised mode.
	bad := watchModel{layoutMode: "bogus"}
	bad.cycleLayout()
	if bad.layoutMode != "horizontal" {
		t.Errorf("cycleLayout fallback = %q", bad.layoutMode)
	}

	// cycleMethod resets an out-of-range cursor to zero.
	cm := newWatchModel("t", "horizontal")
	cm.exchanges = []*exchange{{reqID: "1", method: "GET"}}
	cm.cursor = 5
	cm.methodFilter = "POST" // no POST exchanges → filtered() is empty
	cm.cycleMethod()
	if cm.cursor != 0 {
		t.Errorf("cursor should reset when past filtered length, got %d", cm.cursor)
	}

	// handleFrame skips non-matching exchanges when matching a response.
	fm := newWatchModel("t", "horizontal")
	fm.handleFrame(map[string]any{"type": "http_req", "id": "a", "method": "GET", "url": "/a"})
	fm.handleFrame(map[string]any{"type": "http_req", "id": "b", "method": "GET", "url": "/b"})
	fm.handleFrame(map[string]any{"type": "http_res", "id": "a", "status": 204.0})
	if fm.exchanges[0].status == nil || *fm.exchanges[0].status != 204 {
		t.Fatalf("response did not attach to the earlier exchange: %+v", fm.exchanges[0])
	}
	if fm.exchanges[1].status != nil {
		t.Errorf("later exchange should be untouched, got %+v", fm.exchanges[1])
	}
}

// TestWatchModelInitNil documents that the pure model starts no command.
func TestWatchModelInitNil(t *testing.T) {
	if cmd := newWatchModel("t", "horizontal").Init(); cmd != nil {
		t.Errorf("Init should return no command, got %v", cmd)
	}
}

// TestParseOneHTTPFrameShortHeader covers the malformed-header early return when
// the 8-byte length header is truncated at the end of the buffer.
func TestParseOneHTTPFrameShortHeader(t *testing.T) {
	if _, _, ok := parseOneHTTPFrame("\x10\x02abc", 0); ok {
		t.Error("truncated header should report not-ok")
	}
	// Header present but the length separator ':' is missing.
	if _, _, ok := parseOneHTTPFrame("\x10\x0200000008X", 0); ok {
		t.Error("missing ':' separator should report not-ok")
	}
}

// TestReadWatchTokenEmpty covers the empty token-file branch (no token at all).
func TestReadWatchTokenEmpty(t *testing.T) {
	if got := readWatchToken("", ""); got != "" {
		t.Errorf("no token/file should yield empty, got %q", got)
	}
}

// TestRunWatchNilContext covers the nil-context guard: runWatch defaults to
// context.Background() before validating, which then fails on a bare id.
func TestRunWatchNilContext(t *testing.T) {
	//nolint:staticcheck // deliberately passing a nil context to exercise the guard
	if err := runWatch(nil, "bare-id", "", "horizontal", "", ""); err == nil {
		t.Fatal("bare id without --server should error even with a nil context")
	}
}

// TestParseHex8Cases covers the lower- and upper-case hex digit branches.
func TestParseHex8Cases(t *testing.T) {
	v, err := parseHex8("0000000f")
	if err != nil || v != 15 {
		t.Fatalf("lowercase hex = %d err=%v", v, err)
	}
	v, err = parseHex8("0000000A")
	if err != nil || v != 10 {
		t.Fatalf("uppercase hex = %d err=%v", v, err)
	}
	if _, err := parseHex8("0000000g"); err == nil {
		t.Error("invalid digit should error")
	}
}
