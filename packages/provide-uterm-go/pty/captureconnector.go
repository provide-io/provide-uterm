//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"context"
	"fmt"
	"net"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"

	ptel "github.com/provide-io/provide-telemetry/go"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/screen"
)

// captureBufferCap bounds the CaptureConnector rendered buffer. Port of
// capture_connector.py's 65536 cap.
const captureBufferCap = 65536

// captureConnectorKeys is the accepted connector_config key set. Port of
// capture_connector.py _VALID_CONFIG_KEYS.
var captureConnectorKeys = map[string]struct{}{
	"socket_path": {}, "cols": {}, "rows": {}, "connect_timeout_s": {},
	"input_mode": {}, "stdin_socket_path": {}, "socket_mode": {},
}

// loginPidRE matches a capture session's id (pam-<user>-capture-<pid>-<tag>)
// and captures the pid. The 0x06 input channel's peer check needs it: the
// producer announcing a keystroke socket must be that pid or a descendant.
var loginPidRE = regexp.MustCompile(`^pam-[^-]+-capture-(\d+)-`)

// loginPidFromSessionID extracts the login pid a capture session's id names.
// Port of capture_connector._login_pid_from_session_id.
func loginPidFromSessionID(sessionID string) (int, bool) {
	m := loginPidRE.FindStringSubmatch(sessionID)
	if m == nil {
		return 0, false
	}
	pid, err := strconv.Atoi(m[1])
	if err != nil {
		return 0, false
	}
	return pid, true
}

// expectedStdinSocketPath returns the keystroke-socket path the convention
// allows for this capture socket: the capture socket with .sock replaced by
// .stdin.sock (Black Betty's remote_input_socket_from_capture_socket). Port
// of capture_connector._expected_stdin_socket_path.
func expectedStdinSocketPath(captureSocket string) string {
	if strings.HasSuffix(captureSocket, ".sock") {
		return captureSocket[:len(captureSocket)-len(".sock")] + ".stdin.sock"
	}
	return captureSocket + ".stdin.sock"
}

// isDescendantOf reports whether pid is ancestor or a descendant of it, walking
// /proc/<pid>/stat parent links (field 4 is ppid), at most 64 steps. Returns
// false when /proc is unavailable (e.g. macOS). Port of
// capture_connector._is_descendant_of. A variable so a test can stub it.
var isDescendantOf = func(pid, ancestor int) bool {
	if pid == ancestor {
		return true
	}
	current := pid
	for i := 0; i < 64; i++ {
		data, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", current))
		if err != nil {
			return false
		}
		// Field 2 (comm) is in parens and may contain spaces, so split on the
		// last ')' and parse the rest. rest[0] is state (field 3), rest[1] is
		// ppid (field 4).
		s := string(data)
		idx := strings.LastIndex(s, ")")
		if idx < 0 {
			return false
		}
		rest := strings.Fields(s[idx+1:])
		if len(rest) < 2 {
			return false
		}
		ppid, err := strconv.Atoi(rest[1])
		if err != nil {
			return false
		}
		if ppid == ancestor {
			return true
		}
		if ppid <= 1 {
			return false
		}
		current = ppid
	}
	return false
}

// CaptureConnector observes an LD_PRELOAD/DYLD-captured shell over a Unix socket
// (no process is forked). connector_type = "pty_capture". Port of
// capture_connector.CaptureConnector.
//
// Only CHANNEL_STDOUT contributes to the visible screen; CHANNEL_STDIN and
// CHANNEL_CONNECT frames are recorded for the analysis log. CHANNEL_GEOMETRY,
// sent by whoever owns the pty, sets the session's size and repaints it with a
// snapshot.
type CaptureConnector struct {
	sessionID       string
	displayName     string
	socketPath      string
	socketMode      int
	cols            int
	rows            int
	connectTimeout  float64
	stdinSocketPath string
	// configStdinSocketPath is the path the config named (at construction or
	// via reconfigure). An admin-set path wins over a 0x06 announcement: if
	// this is non-empty, 0x06 frames are a no-op. (Decision 2.)
	configStdinSocketPath string

	mu          sync.Mutex
	capture     *CaptureSocket
	connected   bool
	buffer      string
	pending     string
	connectLog  []string
	stdinCount  int
	stdinWriter net.Conn
	// decoder is the session's one UTF-8 decoder for its output stream. The
	// shim sends each write() as it happens, so a multibyte character can be
	// split across two frames; decoding each frame on its own would turn both
	// halves into U+FFFD. Port of capture_connector.py's _decoder.
	decoder screen.UTF8Decoder
}

// NewCaptureConnector validates config and builds a capture connector. Port of
// CaptureConnector.__init__.
func NewCaptureConnector(sessionID, displayName string, config map[string]any) (*CaptureConnector, error) {
	if err := checkUnknownKeys("CaptureConnector", config, captureConnectorKeys); err != nil {
		return nil, err
	}
	if _, ok := config["socket_path"]; !ok {
		return nil, fmt.Errorf("CaptureConnector requires 'socket_path' in connector_config")
	}
	socketPath := fmt.Sprintf("%v", config["socket_path"])
	stdinPath, _ := optString(config, "stdin_socket_path")
	// The permissions the socket is bound with. The server sets it from
	// pam.capture_socket_mode on every capture session, over anything the
	// stored config says.
	socketMode := OwnerOnlySocketMode
	if v, ok := config["socket_mode"]; ok {
		m, err := CheckSocketMode(v)
		if err != nil {
			return nil, err
		}
		socketMode = m
	}

	timeout := 5.0
	switch t := config["connect_timeout_s"].(type) {
	case float64:
		timeout = t
	case int:
		timeout = float64(t)
	}

	return &CaptureConnector{
		sessionID:             sessionID,
		displayName:           displayName,
		socketPath:            socketPath,
		socketMode:            socketMode,
		cols:                  coerceIntOr(config["cols"], 80),
		rows:                  coerceIntOr(config["rows"], 24),
		connectTimeout:        timeout,
		stdinSocketPath:       stdinPath,
		configStdinSocketPath: stdinPath,
	}, nil
}

// Start binds the capture socket. Port of CaptureConnector.start.
func (c *CaptureConnector) Start(ctx context.Context) error {
	cs, err := NewCaptureSocketWithMode(c.socketPath, c.socketMode)
	if err != nil {
		return err
	}
	if err := cs.Start(); err != nil {
		return err
	}
	c.mu.Lock()
	c.capture = cs
	c.connected = true
	c.mu.Unlock()
	return nil
}

// Stop closes the stdin forwarder and the capture socket. Port of
// CaptureConnector.stop.
func (c *CaptureConnector) Stop(ctx context.Context) error {
	c.closeStdinWriter()
	c.mu.Lock()
	cs := c.capture
	c.capture = nil
	c.connected = false
	c.mu.Unlock()
	if cs != nil {
		return cs.Stop()
	}
	return nil
}

// IsConnected reports the connection state. Port of CaptureConnector.is_connected.
func (c *CaptureConnector) IsConnected() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.connected
}

// PollMessages drains all immediately-available capture frames, updating the
// buffer/analysis state, and returns a "term" frame with the newly captured
// stdout, then a "snapshot" frame if a geometry frame changed the size (or
// nil). Port of CaptureConnector.poll_messages.
func (c *CaptureConnector) PollMessages() []Frame {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.connected || c.capture == nil {
		return nil
	}
	changed := false
	wantCols, wantRows := c.cols, c.rows
	// newStdinPath tracks a 0x06 input frame's effect. nil = no 0x06 this
	// drain; empty string = withdraw; non-empty = a path the convention accepts.
	var newStdinPath *string
	for {
		frame, ok := c.capture.ReadNowait()
		if !ok {
			break
		}
		switch frame.Channel {
		case ChannelStdout:
			// Normalize bare \n → \r\n: capture bypasses the PTY ONLCR driver,
			// so xterm.js would otherwise advance the cursor without a CR.
			text := c.decoder.Decode(frame.Data)
			text = strings.ReplaceAll(text, "\r\n", "\n")
			text = strings.ReplaceAll(text, "\n", "\r\n")
			c.buffer += text
			if len(c.buffer) > captureBufferCap {
				c.buffer = c.buffer[len(c.buffer)-captureBufferCap:]
			}
			c.pending += text
			changed = true
		case ChannelStdin:
			c.stdinCount++
		case ChannelConnect:
			c.connectLog = append(c.connectLog, screen.DecodeUTF8(frame.Data))
			if len(c.connectLog) > 100 {
				c.connectLog = c.connectLog[len(c.connectLog)-100:]
			}
		case ChannelGeometry:
			// The terminal's size, from whoever owns the pty. Malformed is
			// ignored. This connector renders no screen (its snapshot is the
			// raw output tail), so taking a size is recording it; the hosted
			// session's emulator is redrawn at it by connectors.CaptureTransport,
			// which reads the snapshot this returns.
			if cols, rows, ok := ParseGeometry(frame.Data); ok {
				wantCols, wantRows = cols, rows
			}
		case ChannelInput:
			// The producer's keystroke-socket path. An admin-set path in the
			// config wins over an announcement.
			if c.configStdinSocketPath != "" {
				continue
			}
			announced, ok := ParseInputPath(frame.Data)
			if !ok {
				// Malformed: ignore the frame.
				continue
			}
			if announced == "" {
				// Withdraw the path.
				empty := ""
				newStdinPath = &empty
				continue
			}
			// Validate the path convention: the capture socket with .sock
			// replaced by .stdin.sock.
			expected := expectedStdinSocketPath(c.socketPath)
			if announced != expected {
				ptel.GetLogger(context.Background(), "provide.uterm.pty").Warn(
					"capture_input_refused", "reason", "path",
					"announced", announced, "expected", expected)
				continue
			}
			if frame.PeerPid == 0 {
				// Platform without SO_PEERCRED (macOS) or getsockopt failed.
				ptel.GetLogger(context.Background(), "provide.uterm.pty").Warn(
					"capture_input_refused", "reason", "peer_unverifiable")
				continue
			}
			loginPid, found := loginPidFromSessionID(c.sessionID)
			if !found || !isDescendantOf(frame.PeerPid, loginPid) {
				ptel.GetLogger(context.Background(), "provide.uterm.pty").Warn(
					"capture_input_refused", "reason", "peer_mismatch",
					"peer_pid", frame.PeerPid, "login_pid", loginPid)
				continue
			}
			p := announced
			newStdinPath = &p
		}
	}
	// The last valid size of the drain, as Python applies it: once, and only
	// if it differs from the size the drain started at.
	resized := wantCols != c.cols || wantRows != c.rows
	c.cols, c.rows = wantCols, wantRows
	// Apply the 0x06 announcement, if one arrived and changed the path.
	if newStdinPath != nil {
		c.stdinSocketPath = *newStdinPath
		// The next keystroke connects to the new socket (or no socket, if the
		// path was withdrawn). Close any open writer so it reconnects. Inlined
		// (not c.closeStdinWriter()) because we already hold c.mu — calling
		// closeStdinWriter here would deadlock on the re-entrant Lock.
		if c.stdinWriter != nil {
			w := c.stdinWriter
			c.stdinWriter = nil
			// Close outside the critical section would need an unlock/relock
			// dance; Close on a Unix conn is cheap and non-blocking enough to
			// do under the lock, matching the Python path.
			_ = w.Close()
		}
	}
	var frames []Frame
	if changed && c.pending != "" {
		data := c.pending
		c.pending = ""
		frames = append(frames, Frame{"type": "term", "data": data})
	}
	if resized {
		// After the output, which the snapshot already contains; the snapshot
		// carries cols and rows, which is how every viewer learns the size.
		frames = append(frames, c.snapshotLocked())
	}
	return frames
}

// HandleInput forwards keystrokes to the stdin socket when configured. Port of
// CaptureConnector.handle_input.
func (c *CaptureConnector) HandleInput(ctx context.Context, data string) []Frame {
	if c.stdinSocketPath != "" {
		c.forwardStdin([]byte(data))
	}
	return nil
}

// forwardStdin lazily connects the stdin socket, writes, and reconnects+retries
// once on error. Port of CaptureConnector._forward_stdin.
func (c *CaptureConnector) forwardStdin(data []byte) {
	for attempt := 0; attempt < 2; attempt++ {
		c.mu.Lock()
		if c.stdinWriter == nil {
			conn, err := net.Dial("unix", c.stdinSocketPath)
			if err != nil {
				c.mu.Unlock()
				return
			}
			c.stdinWriter = conn
		}
		w := c.stdinWriter
		c.mu.Unlock()

		if _, err := w.Write(data); err == nil {
			return
		}
		c.closeStdinWriter()
	}
}

// closeStdinWriter closes the stdin forwarding connection, swallowing errors.
// Port of CaptureConnector._close_stdin_writer.
func (c *CaptureConnector) closeStdinWriter() {
	c.mu.Lock()
	w := c.stdinWriter
	c.stdinWriter = nil
	c.mu.Unlock()
	if w != nil {
		_ = w.Close()
	}
}

// HandleControl is a no-op for the capture connector. Port of
// CaptureConnector.handle_control.
func (c *CaptureConnector) HandleControl(action string) []Frame { return nil }

// Size reports the terminal size the connector holds: the configured one
// until a geometry frame says otherwise.
func (c *CaptureConnector) Size() (cols, rows int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.cols, c.rows
}

// SetSize puts the terminal at cols x rows, as a geometry frame does, without
// a snapshot: for a session re-created after a restart at the size it had.
func (c *CaptureConnector) SetSize(cols, rows int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.cols, c.rows = cols, rows
}

// GetSnapshot returns a fresh snapshot frame. Port of CaptureConnector.get_snapshot.
func (c *CaptureConnector) GetSnapshot() Frame {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.snapshotLocked()
}

// SetMode announces the input mode it is given. Port of
// CaptureConnector.set_mode. The hub applies the hello's mode, so a constant
// "open" here overrode a session defined as hijack.
func (c *CaptureConnector) SetMode(mode string) []Frame {
	return []Frame{{"type": "worker_hello", "input_mode": mode}}
}

// Clear resets buffer + pending and returns an empty term frame. Port of
// CaptureConnector.clear.
func (c *CaptureConnector) Clear() []Frame {
	c.mu.Lock()
	c.buffer = ""
	c.pending = ""
	c.mu.Unlock()
	return []Frame{{"type": "term", "data": ""}}
}

// GetAnalysis returns a human-readable analysis string. Port of
// CaptureConnector.get_analysis.
func (c *CaptureConnector) GetAnalysis() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	s := fmt.Sprintf(
		"CaptureConnector socket=%q connected=%t buffer_len=%d stdin_keystrokes=%d outbound_connections=%d",
		c.socketPath, c.connected, len(c.buffer), c.stdinCount, len(c.connectLog),
	)
	if len(c.connectLog) > 0 {
		s += fmt.Sprintf(" recent_connect=%q", c.connectLog[len(c.connectLog)-1])
	}
	return s
}

// snapshotLocked builds a snapshot frame. Caller must hold c.mu. Port of
// CaptureConnector._snapshot.
func (c *CaptureConnector) snapshotLocked() Frame {
	screen := c.buffer
	return Frame{
		"type":               "snapshot",
		"screen":             screen,
		"cursor":             map[string]int{"row": 0, "col": 0},
		"cols":               c.cols,
		"rows":               c.rows,
		"screen_hash":        md5Hex(screen),
		"cursor_at_end":      true,
		"has_trailing_space": false,
		"prompt_detected":    false,
		"ts":                 nowTS(),
	}
}
