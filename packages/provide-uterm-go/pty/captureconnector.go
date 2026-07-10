//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"context"
	"fmt"
	"net"
	"strings"
	"sync"
)

// captureBufferCap bounds the CaptureConnector rendered buffer. Port of
// capture_connector.py's 65536 cap.
const captureBufferCap = 65536

// captureConnectorKeys is the accepted connector_config key set. Port of
// capture_connector.py _VALID_CONFIG_KEYS.
var captureConnectorKeys = map[string]struct{}{
	"socket_path": {}, "cols": {}, "rows": {}, "connect_timeout_s": {},
	"input_mode": {}, "stdin_socket_path": {},
}

// CaptureConnector observes an LD_PRELOAD/DYLD-captured shell over a Unix socket
// (no process is forked). connector_type = "pty_capture". Port of
// capture_connector.CaptureConnector.
//
// Only CHANNEL_STDOUT contributes to the visible screen; CHANNEL_STDIN and
// CHANNEL_CONNECT frames are recorded for the analysis log.
type CaptureConnector struct {
	sessionID       string
	displayName     string
	socketPath      string
	cols            int
	rows            int
	connectTimeout  float64
	stdinSocketPath string

	mu          sync.Mutex
	capture     *CaptureSocket
	connected   bool
	buffer      string
	pending     string
	connectLog  []string
	stdinCount  int
	stdinWriter net.Conn
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

	timeout := 5.0
	switch t := config["connect_timeout_s"].(type) {
	case float64:
		timeout = t
	case int:
		timeout = float64(t)
	}

	return &CaptureConnector{
		sessionID:       sessionID,
		displayName:     displayName,
		socketPath:      socketPath,
		cols:            coerceIntOr(config["cols"], 80),
		rows:            coerceIntOr(config["rows"], 24),
		connectTimeout:  timeout,
		stdinSocketPath: stdinPath,
	}, nil
}

// Start binds the capture socket. Port of CaptureConnector.start.
func (c *CaptureConnector) Start(ctx context.Context) error {
	cs, err := NewCaptureSocket(c.socketPath)
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
// buffer/analysis state, and returns a single "term" frame with the newly
// captured stdout (or nil). Port of CaptureConnector.poll_messages.
func (c *CaptureConnector) PollMessages() []Frame {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.connected || c.capture == nil {
		return nil
	}
	changed := false
	for {
		frame, ok := c.capture.ReadNowait()
		if !ok {
			break
		}
		switch frame.Channel {
		case ChannelStdout:
			// Normalize bare \n → \r\n: capture bypasses the PTY ONLCR driver,
			// so xterm.js would otherwise advance the cursor without a CR.
			text := decodeReplace(frame.Data)
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
			c.connectLog = append(c.connectLog, decodeReplace(frame.Data))
			if len(c.connectLog) > 100 {
				c.connectLog = c.connectLog[len(c.connectLog)-100:]
			}
		}
	}
	if changed && c.pending != "" {
		data := c.pending
		c.pending = ""
		return []Frame{{"type": "term", "data": data}}
	}
	return nil
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

// GetSnapshot returns a fresh snapshot frame. Port of CaptureConnector.get_snapshot.
func (c *CaptureConnector) GetSnapshot() Frame {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.snapshotLocked()
}

// SetMode re-advertises the (always-open) input mode. Port of
// CaptureConnector.set_mode.
func (c *CaptureConnector) SetMode(mode string) []Frame {
	return []Frame{{"type": "worker_hello", "input_mode": "open"}}
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
