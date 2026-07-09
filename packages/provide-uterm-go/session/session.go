//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package session defines the terminal-session interfaces shared by
// transports, emulation, and I/O helpers, plus ports of
// provide.uterm.io (PromptWaiter, InputSender) and provide.uterm.expect
// (SendAndExpect).
package session

import (
	"context"
	"time"
)

// Cursor is a screen cursor position.
type Cursor struct {
	X int `json:"x"`
	Y int `json:"y"`
}

// PromptDetection is the prompt-detector result attached to a snapshot.
// Fields mirror the Python detection dict keys.
type PromptDetection struct {
	PromptID  string         `json:"prompt_id"`
	InputType string         `json:"input_type,omitempty"`
	IsIdle    bool           `json:"is_idle"`
	KVData    map[string]any `json:"kv_data,omitempty"`
	Extra     map[string]any `json:"-"`
}

// Snapshot is the emulated screen state. It mirrors the Python
// TerminalEmulator.get_snapshot() dict.
type Snapshot struct {
	Screen           string           `json:"screen"`
	ScreenHash       string           `json:"screen_hash"`
	Cursor           Cursor           `json:"cursor"`
	Cols             int              `json:"cols"`
	Rows             int              `json:"rows"`
	Term             string           `json:"term"`
	CursorAtEnd      bool             `json:"cursor_at_end"`
	HasTrailingSpace bool             `json:"has_trailing_space"`
	RawTail          string           `json:"raw_tail"`
	CapturedAt       float64          `json:"captured_at"`
	PromptDetected   *PromptDetection `json:"prompt_detected,omitempty"`
}

// Session is the minimal interface expected by PromptWaiter and InputSender
// (the Python io.Session protocol).
type Session interface {
	// WaitForUpdate blocks until new bytes arrive from the remote or the
	// timeout elapses; it reports whether new data arrived.
	WaitForUpdate(ctx context.Context, timeout time.Duration) (bool, error)
	// Snapshot returns the latest screen state without performing network I/O.
	Snapshot() Snapshot
	// Send writes data to the session.
	Send(ctx context.Context, data string) error
}

// ConnectionChecker is optionally implemented by sessions that can report
// their connection state (the Python code duck-types is_connected).
type ConnectionChecker interface {
	IsConnected() bool
}

// IdleReporter is optionally implemented by sessions that can predict when
// the screen will be considered idle (the Python seconds_until_idle hook).
type IdleReporter interface {
	SecondsUntilIdle() float64
}

// ExpectSession is the interface required by SendAndExpect (the Python
// expect.ExpectSession protocol).
type ExpectSession interface {
	Send(ctx context.Context, data string) error
	Snapshot() Snapshot
	ScreenChangeSeq() int
	// WaitForScreenChange blocks until the screen updates beyond since (or
	// any next update when since < 0), reporting whether it changed.
	WaitForScreenChange(ctx context.Context, timeout time.Duration, since int) (bool, error)
}

// sessionConnected mirrors Python _session_is_connected: sessions without an
// is_connected implementation are treated as connected.
func sessionConnected(s any) bool {
	checker, ok := s.(ConnectionChecker)
	if !ok {
		return true
	}
	return checker.IsConnected()
}
