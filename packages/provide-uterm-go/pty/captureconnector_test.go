//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"context"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
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
	// The hub applies the hello's mode, so re-advertising "open" regardless
	// would run a session defined as hijack open. Mirrors the Python fix.
	for _, mode := range []string{"hijack", "open"} {
		c := newCaptureConn(t, nil)
		frames := c.SetMode(mode)
		if len(frames) != 1 || frames[0]["type"] != "worker_hello" || frames[0]["input_mode"] != mode {
			t.Fatalf("set_mode(%q) should announce %q: %+v", mode, mode, frames)
		}
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

// The geometry channel: whoever owns the pty (a supervisor; never the shim,
// which cannot see a resize) says what size the terminal is, as >HH cols, rows.
// The connector takes the size and repaints at once with a snapshot, which is
// how every viewer learns it.

func startedCaptureConn(t *testing.T) *CaptureConnector {
	t.Helper()
	c := newCaptureConn(t, map[string]any{"cols": 80, "rows": 24})
	if err := c.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = c.Stop(context.Background()) })
	return c
}

func geometryPayload(cols, rows uint16) []byte {
	b := make([]byte, 4)
	binary.BigEndian.PutUint16(b, cols)
	binary.BigEndian.PutUint16(b[2:], rows)
	return b
}

func frameKinds(frames []Frame) []string {
	out := make([]string, 0, len(frames))
	for _, f := range frames {
		if f["type"] == "snapshot" {
			out = append(out, fmt.Sprintf("snapshot %vx%v", f["cols"], f["rows"]))
		} else {
			out = append(out, fmt.Sprint(f["type"]))
		}
	}
	return out
}

func TestCaptureConnectorGeometryFrameResizesAndRepaints(t *testing.T) {
	c := startedCaptureConn(t)
	c.capture.enqueue(CaptureFrame{Channel: ChannelStdout, Data: []byte("\x1b[2J\x1b[H" + strings.Repeat("x", 90))})
	c.capture.enqueue(CaptureFrame{Channel: ChannelGeometry, Data: geometryPayload(100, 40)})
	frames := c.PollMessages()
	// The output first, so a viewer streaming it is not handed it after the
	// repaint that already contains it.
	if got := frameKinds(frames); !reflect.DeepEqual(got, []string{"term", "snapshot 100x40"}) {
		t.Fatalf("frames = %v", got)
	}
	if snap := c.GetSnapshot(); snap["cols"] != 100 || snap["rows"] != 40 {
		t.Fatalf("snapshot dims = %vx%v", snap["cols"], snap["rows"])
	}
}

func TestCaptureConnectorGeometryFrameAloneRepaints(t *testing.T) {
	c := startedCaptureConn(t)
	c.capture.enqueue(CaptureFrame{Channel: ChannelGeometry, Data: geometryPayload(132, 50)})
	if got := frameKinds(c.PollMessages()); !reflect.DeepEqual(got, []string{"snapshot 132x50"}) {
		t.Fatalf("frames = %v", got)
	}
}

func TestCaptureConnectorSameGeometryRepaintsNothing(t *testing.T) {
	c := startedCaptureConn(t)
	c.capture.enqueue(CaptureFrame{Channel: ChannelGeometry, Data: geometryPayload(80, 24)})
	if frames := c.PollMessages(); len(frames) != 0 {
		t.Fatalf("frames = %v", frameKinds(frames))
	}
}

func TestCaptureConnectorOnlyTheLastGeometryIsPainted(t *testing.T) {
	c := startedCaptureConn(t)
	c.capture.enqueue(CaptureFrame{Channel: ChannelGeometry, Data: geometryPayload(100, 40)})
	c.capture.enqueue(CaptureFrame{Channel: ChannelGeometry, Data: geometryPayload(120, 30)})
	if got := frameKinds(c.PollMessages()); !reflect.DeepEqual(got, []string{"snapshot 120x30"}) {
		t.Fatalf("frames = %v", got)
	}
}

func TestCaptureConnectorMalformedGeometryIsIgnored(t *testing.T) {
	for name, payload := range map[string][]byte{
		"empty":           {},
		"three bytes":     {0x00, 0x64, 0x00},
		"five bytes":      {0x00, 0x64, 0x00, 0x28, 0x00},
		"cols over limit": geometryPayload(1001, 40),
		"rows over limit": geometryPayload(40, 1001),
		"zero cols":       geometryPayload(0, 40),
	} {
		t.Run(name, func(t *testing.T) {
			if _, _, ok := ParseGeometry(payload); ok {
				t.Fatal("ParseGeometry accepted it")
			}
			c := startedCaptureConn(t)
			c.capture.enqueue(CaptureFrame{Channel: ChannelGeometry, Data: payload})
			if frames := c.PollMessages(); len(frames) != 0 {
				t.Fatalf("frames = %v", frameKinds(frames))
			}
			if c.cols != 80 || c.rows != 24 {
				t.Fatalf("size changed to %dx%d", c.cols, c.rows)
			}
		})
	}
}

func TestCaptureConnectorStatsFrameIsIgnored(t *testing.T) {
	c := startedCaptureConn(t)
	c.capture.enqueue(CaptureFrame{Channel: ChannelStats, Data: []byte("attempts=1")})
	if frames := c.PollMessages(); len(frames) != 0 {
		t.Fatalf("frames = %v", frameKinds(frames))
	}
}

// capture_geometry_frame in spec/behavior_vectors.json, generated from the
// Python reference by scripts/generate_behavior_vectors.py; testdata/ holds a
// byte-identical copy that scripts/check_protocol_drift.py keeps in step.
func TestCaptureConnectorSharedGeometryVectors(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "behavior_vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var vectors struct {
		Geometry []struct {
			Name  string `json:"name"`
			Frame string `json:"frame"`
			Size  *struct {
				Cols int `json:"cols"`
				Rows int `json:"rows"`
			} `json:"size"`
		} `json:"capture_geometry_frame"`
	}
	if err := json.Unmarshal(raw, &vectors); err != nil {
		t.Fatal(err)
	}
	if len(vectors.Geometry) == 0 {
		t.Fatal("no capture_geometry_frame vectors")
	}
	for _, tc := range vectors.Geometry {
		t.Run(tc.Name, func(t *testing.T) {
			frame, err := hex.DecodeString(tc.Frame)
			if err != nil {
				t.Fatal(err)
			}
			// Through the socket's own reader, header and all.
			c := startedCaptureConn(t)
			sendFrames(t, c.socketPath, frame)
			deadline := time.Now().Add(2 * time.Second)
			for c.capture.QueueLen() == 0 && time.Now().Before(deadline) {
				time.Sleep(5 * time.Millisecond)
			}
			got := frameKinds(c.PollMessages())
			want := []string{}
			if tc.Size != nil {
				want = []string{fmt.Sprintf("snapshot %dx%d", tc.Size.Cols, tc.Size.Rows)}
			}
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("frames = %v, want %v", got, want)
			}
		})
	}
}

func TestCaptureConnectorGeometryThereAndBackRepaintsNothing(t *testing.T) {
	c := startedCaptureConn(t)
	c.capture.enqueue(CaptureFrame{Channel: ChannelGeometry, Data: geometryPayload(100, 40)})
	c.capture.enqueue(CaptureFrame{Channel: ChannelGeometry, Data: geometryPayload(80, 24)})
	if frames := c.PollMessages(); len(frames) != 0 {
		t.Fatalf("frames = %v", frameKinds(frames))
	}
}

func TestCaptureConnectorSizeFollowsGeometry(t *testing.T) {
	c := startedCaptureConn(t)
	if cols, rows := c.Size(); cols != 80 || rows != 24 {
		t.Fatalf("Size = %dx%d, want the configured 80x24", cols, rows)
	}
	c.capture.enqueue(CaptureFrame{Channel: ChannelGeometry, Data: geometryPayload(132, 50)})
	c.PollMessages()
	if cols, rows := c.Size(); cols != 132 || rows != 50 {
		t.Fatalf("Size = %dx%d, want 132x50", cols, rows)
	}
}

// SetSize puts the connector at a size, as a geometry frame does, without a
// snapshot.
func TestCaptureConnectorSetSize(t *testing.T) {
	c, err := NewCaptureConnector("s", "n", map[string]any{"socket_path": "/tmp/x.sock"})
	if err != nil {
		t.Fatal(err)
	}
	c.SetSize(120, 40)
	if cols, rows := c.Size(); cols != 120 || rows != 40 {
		t.Fatalf("size %dx%d", cols, rows)
	}
}

// ─── The input channel (0x06) ──────────────────────────────────────────────
//
// A producer that can deliver keystrokes (a supervisor) announces its keystroke
// socket's path on 0x06. The connector validates the path (convention: the
// capture socket with .sock replaced by .stdin.sock), the peer (SO_PEERCRED pid
// must be the login's pid or a descendant), and then sets stdin_socket_path in
// place. An admin-set path in the config wins over any announcement.
// (Decision 2 of the capture-session-ownership ARD.)

const (
	pamSessionID  = "pam-alice-capture-1234-98765"
	pamCapSocket  = "/run/uterm-cap-1234.sock"
	pamStdinSock  = "/run/uterm-cap-1234.stdin.sock"
)

// startedPamCaptureConn creates a connector with a PAM-style session id and a
// capture socket path the convention check (.sock → .stdin.sock) can run on,
// WITHOUT binding the socket (mirrors the Python _fed_input helper). Frames are
// enqueued directly into the capture socket's queue.
func startedPamCaptureConn(t *testing.T) *CaptureConnector {
	t.Helper()
	cfg := map[string]any{"socket_path": pamCapSocket, "cols": 80, "rows": 24}
	c, err := NewCaptureConnector(pamSessionID, "disp", cfg)
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	// Create the capture socket without starting it (no bind). The queue is
	// already allocated; tests enqueue frames directly.
	cs, err := NewCaptureSocket(pamCapSocket)
	if err != nil {
		t.Fatalf("capture socket: %v", err)
	}
	c.mu.Lock()
	c.capture = cs
	c.connected = true
	c.mu.Unlock()
	return c
}

func TestLoginPidFromSessionIDExtractsPid(t *testing.T) {
	pid, ok := loginPidFromSessionID("pam-alice-capture-1234-98765")
	if !ok {
		t.Fatal("expected ok")
	}
	if pid != 1234 {
		t.Fatalf("pid = %d, want 1234", pid)
	}
}

func TestLoginPidFromSessionIDReturnsFalseForNonPam(t *testing.T) {
	if _, ok := loginPidFromSessionID("scratch-1"); ok {
		t.Fatal("expected !ok for non-PAM id")
	}
}

func TestExpectedStdinSocketPathReplacesSockSuffix(t *testing.T) {
	if got := expectedStdinSocketPath("/run/uterm-cap-12345.sock"); got != "/run/uterm-cap-12345.stdin.sock" {
		t.Fatalf("got %q", got)
	}
}

func TestExpectedStdinSocketPathAppendsWhenNoSockSuffix(t *testing.T) {
	if got := expectedStdinSocketPath("/run/cap"); got != "/run/cap.stdin.sock" {
		t.Fatalf("got %q", got)
	}
}

func TestIsDescendantOfSelfIsTrue(t *testing.T) {
	if !isDescendantOf(1234, 1234) {
		t.Fatal("expected self to be a descendant")
	}
}

func TestIsDescendantOfReturnsFalseWhenProcUnavailable(t *testing.T) {
	// A pid that does not exist; the /proc/<pid>/stat read fails.
	if isDescendantOf(99999999, 1234) {
		t.Fatal("expected false for nonexistent pid")
	}
}

func TestCaptureConnectorInputFrameValidPathSetsStdinSocketPath(t *testing.T) {
	// A 0x06 frame with the right path and a valid peer sets stdin_socket_path.
	orig := isDescendantOf
	isDescendantOf = func(pid, ancestor int) bool { return true }
	defer func() { isDescendantOf = orig }()
	c := startedPamCaptureConn(t)
	c.capture.enqueue(CaptureFrame{Channel: ChannelInput, Data: []byte(pamStdinSock), PeerPid: 1234})
	c.PollMessages()
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stdinSocketPath != pamStdinSock {
		t.Fatalf("stdinSocketPath = %q, want %q", c.stdinSocketPath, pamStdinSock)
	}
}

func TestCaptureConnectorInputFrameWrongPathIsRefused(t *testing.T) {
	orig := isDescendantOf
	isDescendantOf = func(pid, ancestor int) bool { return true }
	defer func() { isDescendantOf = orig }()
	c := startedPamCaptureConn(t)
	c.capture.enqueue(CaptureFrame{Channel: ChannelInput, Data: []byte("/run/evil.sock"), PeerPid: 1234})
	c.PollMessages()
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stdinSocketPath != "" {
		t.Fatalf("stdinSocketPath = %q, want empty (refused)", c.stdinSocketPath)
	}
}

func TestCaptureConnectorInputFrameEmptyPayloadWithdrawsPath(t *testing.T) {
	orig := isDescendantOf
	isDescendantOf = func(pid, ancestor int) bool { return true }
	defer func() { isDescendantOf = orig }()
	c := startedPamCaptureConn(t)
	c.mu.Lock()
	c.stdinSocketPath = "/run/old.stdin.sock"
	c.mu.Unlock()
	c.capture.enqueue(CaptureFrame{Channel: ChannelInput, Data: []byte{}, PeerPid: 1234})
	c.PollMessages()
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stdinSocketPath != "" {
		t.Fatalf("stdinSocketPath = %q, want empty (withdrawn)", c.stdinSocketPath)
	}
}

func TestCaptureConnectorInputFramePeerPidZeroIsRefused(t *testing.T) {
	// peer_pid=0 means "unverifiable" (macOS, no SO_PEERCRED); 0x06 is refused.
	orig := isDescendantOf
	isDescendantOf = func(pid, ancestor int) bool { return true }
	defer func() { isDescendantOf = orig }()
	c := startedPamCaptureConn(t)
	c.capture.enqueue(CaptureFrame{Channel: ChannelInput, Data: []byte(pamStdinSock), PeerPid: 0})
	c.PollMessages()
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stdinSocketPath != "" {
		t.Fatalf("stdinSocketPath = %q, want empty (peer unverifiable)", c.stdinSocketPath)
	}
}

func TestCaptureConnectorInputFramePeerMismatchIsRefused(t *testing.T) {
	orig := isDescendantOf
	isDescendantOf = func(pid, ancestor int) bool { return false }
	defer func() { isDescendantOf = orig }()
	c := startedPamCaptureConn(t)
	c.capture.enqueue(CaptureFrame{Channel: ChannelInput, Data: []byte(pamStdinSock), PeerPid: 9999})
	c.PollMessages()
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stdinSocketPath != "" {
		t.Fatalf("stdinSocketPath = %q, want empty (peer mismatch)", c.stdinSocketPath)
	}
}

func TestCaptureConnectorInputFrameAdminSetPathWinsOverAnnouncement(t *testing.T) {
	orig := isDescendantOf
	isDescendantOf = func(pid, ancestor int) bool { return true }
	defer func() { isDescendantOf = orig }()
	adminPath := "/run/admin-set.stdin.sock"
	cfg := map[string]any{"socket_path": pamCapSocket, "stdin_socket_path": adminPath}
	c, err := NewCaptureConnector(pamSessionID, "disp", cfg)
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	cs, err := NewCaptureSocket(pamCapSocket)
	if err != nil {
		t.Fatalf("capture socket: %v", err)
	}
	c.mu.Lock()
	c.capture = cs
	c.connected = true
	c.mu.Unlock()
	c.capture.enqueue(CaptureFrame{Channel: ChannelInput, Data: []byte(pamStdinSock), PeerPid: 1234})
	c.PollMessages()
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stdinSocketPath != adminPath {
		t.Fatalf("stdinSocketPath = %q, want admin path %q (admin wins)", c.stdinSocketPath, adminPath)
	}
}

func TestCaptureConnectorInputFrameMalformedPayloadIsIgnored(t *testing.T) {
	orig := isDescendantOf
	isDescendantOf = func(pid, ancestor int) bool { return true }
	defer func() { isDescendantOf = orig }()
	c := startedPamCaptureConn(t)
	c.capture.enqueue(CaptureFrame{Channel: ChannelInput, Data: []byte("relative/path"), PeerPid: 1234})
	c.PollMessages()
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stdinSocketPath != "" {
		t.Fatalf("stdinSocketPath = %q, want empty (malformed ignored)", c.stdinSocketPath)
	}
}

// capture_input_frame in spec/behavior_vectors.json, generated from the Python
// reference by scripts/generate_behavior_vectors.py; testdata/ holds a
// byte-identical copy that scripts/check_protocol_drift.py keeps in step.
func TestCaptureSharedInputVectors(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "behavior_vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var vectors struct {
		Input []struct {
			Name  string  `json:"name"`
			Frame string  `json:"frame"`
			Path  *string `json:"path"`
		} `json:"capture_input_frame"`
	}
	if err := json.Unmarshal(raw, &vectors); err != nil {
		t.Fatal(err)
	}
	if len(vectors.Input) == 0 {
		t.Fatal("no capture_input_frame vectors")
	}
	for _, tc := range vectors.Input {
		t.Run(tc.Name, func(t *testing.T) {
			// The frame is [1B channel][4B length][payload]; skip the 5-byte
			// header and parse the payload with ParseInputPath, which is what
			// the shared vectors hold the parser to.
			frame, err := hex.DecodeString(tc.Frame)
			if err != nil {
				t.Fatal(err)
			}
			if len(frame) < headerSize {
				t.Fatalf("frame too short: %d bytes", len(frame))
			}
			payload := frame[headerSize:]
			got, _ := ParseInputPath(payload)
			var want string
			if tc.Path != nil {
				want = *tc.Path
			}
			if got != want {
				t.Fatalf("ParseInputPath = %q, want %q", got, want)
			}
		})
	}
}
