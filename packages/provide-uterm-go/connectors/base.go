//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/session"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/termsession"
)

// maxEvents bounds the buffered raw-output ring. It mirrors the Python
// connectors' screen-buffer cap intent: keep recent output without unbounded
// growth from a chatty upstream.
const maxEvents = 256

// buildFunc constructs a fresh, unconnected TransportSession for a connector.
// It is called on each Start so a stopped connector can be started again.
type buildFunc func() *termsession.TransportSession

// transportConnector is the shared Connector implementation for every transport
// (shell/ssh/telnet/websocket). The only per-kind differences are the buildFunc,
// the kind label, the upstream URL string and the analysis extras — all injected
// at construction. It satisfies Connector.
type transportConnector struct {
	sessionID   string
	displayName string
	kind        string // "shell" | "ssh" | "telnet" | "websocket"
	upstream    string // e.g. "ssh://user@host:22" — for analysis
	build       buildFunc

	mu        sync.Mutex
	sess      *termsession.TransportSession
	inputMode string
	paused    bool
	connected bool
	rxBytes   int
	events    []map[string]any
}

var _ Connector = (*transportConnector)(nil)

// newTransportConnector wires the shared connector state.
func newTransportConnector(sessionID, displayName, kind, upstream, inputMode string, build buildFunc) *transportConnector {
	return &transportConnector{
		sessionID:   sessionID,
		displayName: displayName,
		kind:        kind,
		upstream:    upstream,
		inputMode:   inputMode,
		build:       build,
	}
}

// Start builds a fresh session, attaches the raw-output watcher, and dials.
func (c *transportConnector) Start(ctx context.Context) error {
	c.mu.Lock()
	if c.connected && c.sess != nil {
		c.mu.Unlock()
		return nil
	}
	sess := c.build()
	c.mu.Unlock()

	sess.AddWatch(c.onRaw)
	if err := sess.Connect(ctx); err != nil {
		return err
	}

	c.mu.Lock()
	c.sess = sess
	c.connected = true
	c.mu.Unlock()
	return nil
}

// onRaw buffers each raw wire chunk as a term event (the Python poll_messages
// "term" message). It runs in the reader goroutine; TransportSession invokes it
// with its own lock released, so locking c.mu here cannot invert lock order.
func (c *transportConnector) onRaw(_ map[string]any, raw []byte) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.rxBytes += len(raw)
	c.events = append(c.events, map[string]any{
		"type": "term",
		"data": string(raw),
		"ts":   float64(time.Now().UnixNano()) / 1e9,
	})
	if len(c.events) > maxEvents {
		c.events = c.events[len(c.events)-maxEvents:]
	}
}

// Stop tears down the live session. Idempotent.
func (c *transportConnector) Stop(ctx context.Context) error {
	c.mu.Lock()
	sess := c.sess
	c.sess = nil
	c.connected = false
	c.mu.Unlock()
	if sess == nil {
		return nil
	}
	return sess.Close(ctx)
}

// IsConnected reports whether the underlying session is live.
func (c *transportConnector) IsConnected() bool {
	c.mu.Lock()
	sess := c.sess
	connected := c.connected
	c.mu.Unlock()
	return connected && sess != nil && sess.IsConnected()
}

// HandleInput forwards user input to the upstream session.
func (c *transportConnector) HandleInput(ctx context.Context, data string) error {
	c.mu.Lock()
	sess := c.sess
	connected := c.connected
	c.mu.Unlock()
	if !connected || sess == nil {
		return nil
	}
	return sess.Send(ctx, data)
}

// HandleControl applies a hijack control action. Unknown actions are ignored
// (mirrors the Python connectors, which only log/annotate them).
func (c *transportConnector) HandleControl(action string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	switch action {
	case "pause":
		c.paused = true
	case "resume":
		c.paused = false
	case "step":
		// no upstream state to advance; a no-op awaiting output
	}
	return nil
}

// SetMode switches the input mode. Switching to "open" also clears the paused
// (hijack) flag, matching the Python set_mode.
func (c *transportConnector) SetMode(mode string) error {
	if mode != "open" && mode != "hijack" {
		return fmt.Errorf("invalid mode: %s", mode)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.inputMode = mode
	if mode == "open" {
		c.paused = false
	}
	return nil
}

// Clear resets the emulated screen. It writes a clear sequence to the local
// emulator only (no bytes go upstream), matching the Python clear() which resets
// the local screen buffer.
func (c *transportConnector) Clear() error {
	c.mu.Lock()
	sess := c.sess
	c.events = nil
	c.mu.Unlock()
	if sess != nil {
		sess.Emulator().Process([]byte("\x1b[2J\x1b[H"))
	}
	return nil
}

// Snapshot returns the current emulated screen state.
func (c *transportConnector) Snapshot() session.Snapshot {
	c.mu.Lock()
	sess := c.sess
	c.mu.Unlock()
	if sess == nil {
		return session.Snapshot{}
	}
	return sess.Snapshot()
}

// Analysis returns a human-readable analysis string mirroring get_analysis.
func (c *transportConnector) Analysis() string {
	c.mu.Lock()
	inputMode := c.inputMode
	paused := c.paused
	rx := c.rxBytes
	c.mu.Unlock()
	lines := []string{
		fmt.Sprintf("[%s session analysis — worker: %s]", c.kind, c.sessionID),
		fmt.Sprintf("upstream: %s", c.upstream),
		fmt.Sprintf("input_mode: %s", inputMode),
		fmt.Sprintf("paused: %t", paused),
		fmt.Sprintf("bytes_received: %d", rx),
		fmt.Sprintf("connected: %t", c.IsConnected()),
	}
	return strings.Join(lines, "\n")
}

// Events returns a copy of the buffered raw-output events.
func (c *transportConnector) Events() []map[string]any {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]map[string]any, len(c.events))
	copy(out, c.events)
	return out
}

// Session exposes the live TransportSession, or nil when not connected.
func (c *transportConnector) Session() *termsession.TransportSession {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.sess
}
