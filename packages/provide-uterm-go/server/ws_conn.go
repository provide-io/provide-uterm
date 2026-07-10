//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"sync"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

// wsBase serializes writes to a coder/websocket connection, which requires that
// only one goroutine writes at a time. The hub pushes frames from its router
// goroutines while the handler's send paths also write, so every write goes
// through writeMu.
type wsBase struct {
	conn    *websocket.Conn
	writeMu sync.Mutex
}

// SendText writes an already-control-framed payload as a WS text message. This
// satisfies hub.BrowserSender and hub.WorkerWS.
func (b *wsBase) SendText(ctx context.Context, payload string) error {
	b.writeMu.Lock()
	defer b.writeMu.Unlock()
	return b.conn.Write(ctx, websocket.MessageText, []byte(payload))
}

// workerConn adapts a worker WebSocket to hub.WorkerWS + hub.WorkerCloser.
type workerConn struct{ wsBase }

// Close closes the worker socket (hub.WorkerCloser).
func (c *workerConn) Close(_ context.Context) error {
	return c.conn.Close(websocket.StatusNormalClosure, "")
}

// browserConn adapts a browser WebSocket to hub.BrowserSender + hub.BrowserCloser.
// It also carries the resolved principal so the hub's identity provider (when
// wired) can read it via UtermPrincipal.
type browserConn struct {
	wsBase
	principal *serverauth.Principal
}

// Close closes the browser socket with a WS close code (hub.BrowserCloser).
func (c *browserConn) Close(_ context.Context, code int, reason string) error {
	return c.conn.Close(websocket.StatusCode(code), reason)
}

// UtermPrincipal exposes the resolved principal to the hub identity provider
// (used for output redaction / role-scoped broadcasts when a provider is wired).
func (c *browserConn) UtermPrincipal() any {
	if c.principal == nil {
		return nil
	}
	roles := make(map[string]bool, len(c.principal.Roles))
	for role := range c.principal.Roles {
		roles[role] = true
	}
	return &hub.Principal{SubjectID: c.principal.SubjectID, Roles: roles, Claims: c.principal.Claims}
}
