//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"context"
	"crypto/md5" //nolint:gosec // non-crypto change-detection hash (matches Python md5)
	"encoding/hex"
	"fmt"
	"runtime"
)

// PollMessages returns a snapshot frame when new output has arrived since the
// last poll, else nil. Returns nil while paused or flow-paused. Port of
// PTYConnector.poll_messages.
func (c *PTYConnector) PollMessages() []Frame {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.connected || c.master == nil || c.paused || c.flowPaused {
		return nil
	}
	if !c.dirty {
		return nil
	}
	c.dirty = false
	return []Frame{c.snapshotLocked()}
}

// HandleInput writes keystrokes to the PTY master (unless paused/disconnected)
// and returns a snapshot. Port of PTYConnector.handle_input.
func (c *PTYConnector) HandleInput(ctx context.Context, data string) []Frame {
	c.mu.Lock()
	master := c.master
	writable := c.connected && master != nil && !c.paused
	c.mu.Unlock()

	if writable {
		// Write outside the lock: a full PTY buffer must not block Stop (which
		// takes the lock to close master) — that would deadlock.
		if _, err := master.Write([]byte(data)); err != nil {
			// The PTY master flips to EIO/EPIPE the instant the child exits.
			// Mirror readLoop: mark disconnected rather than raising out.
			c.mu.Lock()
			c.connected = false
			c.mu.Unlock()
		}
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	return []Frame{c.snapshotLocked()}
}

// HandleControl applies hijack pause/resume/step and flow_pause/flow_resume
// backpressure. Port of PTYConnector.handle_control.
func (c *PTYConnector) HandleControl(action string) []Frame {
	c.mu.Lock()
	defer c.mu.Unlock()
	switch action {
	case "pause":
		c.paused = true
	case "resume", "step":
		c.paused = false
	case "flow_pause":
		// Backpressure XOFF: stop surfacing output. Emit nothing.
		c.flowPaused = true
		return nil
	case "flow_resume":
		c.flowPaused = false
		return nil
	}
	return []Frame{c.snapshotLocked()}
}

// GetSnapshot returns a fresh snapshot frame. Port of PTYConnector.get_snapshot.
func (c *PTYConnector) GetSnapshot() Frame {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.snapshotLocked()
}

// GetAnalysis returns a human-readable analysis string. Port of
// PTYConnector.get_analysis.
func (c *PTYConnector) GetAnalysis() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return fmt.Sprintf(
		"PTYConnector command=%q connected=%t paused=%t inject=%t cols=%d rows=%d buffer_len=%d",
		c.command, c.connected, c.paused, c.inject, c.cols, c.rows, len(c.buffer),
	)
}

// SetMode changes the input mode and re-advertises it. Port of
// PTYConnector.set_mode.
func (c *PTYConnector) SetMode(mode string) ([]Frame, error) {
	if _, ok := validModes[mode]; !ok {
		return nil, fmt.Errorf("invalid mode %q: must be one of %v", mode, sortedModes())
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.inputMode = mode
	return []Frame{c.helloLocked(), c.snapshotLocked()}, nil
}

// Clear resets the rendered buffer (leaving the incremental decoder's partial
// sequence intact) and returns a snapshot. Port of PTYConnector.clear.
func (c *PTYConnector) Clear() []Frame {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.buffer = ""
	return []Frame{c.snapshotLocked()}
}

// snapshotLocked builds a snapshot frame. Caller must hold c.mu. Port of
// PTYConnector._snapshot.
func (c *PTYConnector) snapshotLocked() Frame {
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

// helloLocked builds a worker_hello frame. Caller must hold c.mu. Port of
// PTYConnector._hello.
func (c *PTYConnector) helloLocked() Frame {
	return Frame{"type": "worker_hello", "input_mode": c.inputMode}
}

// md5Hex returns the lowercase hex MD5 of s (non-crypto change-detection hash,
// matching Python's hashlib.md5(screen.encode()).hexdigest()).
func md5Hex(s string) string {
	sum := md5.Sum([]byte(s)) //nolint:gosec // change-detection hash, not security
	return hex.EncodeToString(sum[:])
}

// isDarwin reports whether the host is macOS (for DYLD vs LD_PRELOAD selection).
func isDarwin() bool {
	return runtime.GOOS == "darwin"
}
