//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package connectors is the Go port of the provide-uterm server session
// connector layer (packages/provide-uterm-server/.../connectors). It provides a
// Connector interface mirroring the Python SessionConnector ABC (base.py) plus
// the four built-in implementations — shell, ssh, telnet, websocket — each of
// which drives a live termsession.TransportSession (and thus a session.Session)
// over the matching transports.ConnectionTransport.
//
// Deviation from Python: the Python ShellSessionConnector is an in-memory
// reference chat connector; the Go shell connector instead spawns a real local
// shell in a PTY (creack/pty), because the Go port is used to actually host
// terminals. The transport connectors also run a background reader goroutine
// (inside TransportSession) rather than the Python poll_messages pull loop, so
// Events() surfaces the buffered raw chunks that the Python poll_messages would
// have returned.
package connectors

import (
	"context"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/session"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/termsession"
)

// Connector is the abstraction the hosted session runtime drives. It is the Go
// port of provide.uterm.server.connectors.base.SessionConnector. Every method
// maps to a Python coroutine of the same intent:
//
//	Start        -> start
//	Stop         -> stop
//	IsConnected  -> is_connected
//	HandleInput  -> handle_input
//	HandleControl-> handle_control
//	SetMode      -> set_mode
//	Clear        -> clear
//	Snapshot     -> get_snapshot
//	Analysis     -> get_analysis
//	Events       -> poll_messages (buffered, not pulled)
//
// Session exposes the live TransportSession (nil until Start succeeds) so the
// server registry can read snapshots and route I/O through one object.
type Connector interface {
	// Start opens the upstream session (dials the transport, starts the reader).
	Start(ctx context.Context) error
	// Stop tears the upstream session down. Safe to call when never started.
	Stop(ctx context.Context) error
	// IsConnected reports whether the connector currently has a live link.
	IsConnected() bool
	// HandleInput forwards user input to the upstream session.
	HandleInput(ctx context.Context, data string) error
	// HandleControl applies a control action ("pause"/"resume"/"step").
	HandleControl(action string) error
	// SetMode switches the input mode ("open"/"hijack"). Invalid → error.
	SetMode(mode string) error
	// Clear resets the emulated screen.
	Clear() error
	// Snapshot returns the current emulated screen state (zero value if not started).
	Snapshot() session.Snapshot
	// Analysis returns a human-readable multi-line analysis string.
	Analysis() string
	// Events returns the buffered raw-output events observed since Start.
	Events() []map[string]any
	// Session exposes the live TransportSession, or nil when not connected.
	Session() *termsession.TransportSession
}
