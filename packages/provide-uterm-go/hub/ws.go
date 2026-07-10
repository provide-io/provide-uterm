//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "context"

// BrowserSender is the outbound surface of a dashboard-browser WebSocket used
// by the message router. The Python code calls “ws.send_text(...)“ on a
// FastAPI WebSocket; this port models exactly that one method so tests supply
// fakes and the future server wires a coder/websocket conn.
//
// A [BrowserConn] stored in a worker state's browser map is a comparable
// identity (used as a map key). To actually send to it the router type-asserts
// it to BrowserSender; a browser conn that does not implement BrowserSender is
// treated as a dead socket and pruned (it can never receive a frame).
type BrowserSender interface {
	// SendText writes an already-encoded frame to the browser. ctx carries the
	// per-send deadline (the Go analogue of asyncio.wait_for's timeout).
	SendText(ctx context.Context, payload string) error
}

// BrowserCloser is the optional close surface of a browser WebSocket, used by
// the behavioral-audit deny path (which closes the connection with a policy
// code). A browser conn that does not implement it is not closed.
type BrowserCloser interface {
	// Close closes the browser connection with a WebSocket close code and reason.
	Close(ctx context.Context, code int, reason string) error
}

// WorkerCloser is the optional close surface of a worker WebSocket, used by
// [TermHub.DisconnectWorker] to programmatically tear the worker socket down.
// A worker WS that does not implement it is simply detached without a close.
type WorkerCloser interface {
	// Close closes the worker connection.
	Close(ctx context.Context) error
}

// TunnelSender is the binary-tunnel outbound surface. A worker whose state has
// IsTunnelWorker=true and whose socket implements TunnelSender receives input
// as raw PTY bytes (SendInput) and HTTP-inspect controls on the HTTP side
// channel (SendHTTPControl); every other message type is dropped, matching the
// Python tunnel routing in router_broadcast.send_worker.
//
// Deviation: the Python path frames the HTTP control with the tunnel wire
// protocol (encode_frame(CHANNEL_HTTP, json)). No Go tunnel-protocol package
// exists yet, so this port defers the framing to the socket implementation
// (the server wave provides it); the router only makes the routing decision.
type TunnelSender interface {
	SendInput(ctx context.Context, data string) error
	SendHTTPControl(ctx context.Context, msg map[string]any) error
}
