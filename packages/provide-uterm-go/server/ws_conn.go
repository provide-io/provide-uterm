//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"errors"
	"sync"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/deckmux"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

// wsBase serializes writes to a coder/websocket connection, which requires that
// only one goroutine writes at a time. The hub pushes frames from its router
// goroutines while the handler's send paths also write, so every write goes
// through writeMu.
type wsBase struct {
	conn      *websocket.Conn
	writeOnce sync.Once
	writeGate chan struct{}
}

func (b *wsBase) withWrite(ctx context.Context, fn func() error) error {
	b.writeOnce.Do(func() {
		b.writeGate = make(chan struct{}, 1)
		b.writeGate <- struct{}{}
	})
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-b.writeGate:
	}
	defer func() { b.writeGate <- struct{}{} }()
	return fn()
}

// SendText writes an already-control-framed payload as a WS text message. This
// satisfies hub.BrowserSender and hub.WorkerWS.
func (b *wsBase) SendText(ctx context.Context, payload string) error {
	return b.withWrite(ctx, func() error {
		return b.conn.Write(ctx, websocket.MessageText, []byte(payload))
	})
}

// workerConn adapts a worker WebSocket to hub.WorkerWS + hub.WorkerCloser.
type workerConn struct{ wsBase }

// Close closes the worker socket (hub.WorkerCloser).
func (c *workerConn) Close(_ context.Context) error {
	return c.conn.Close(websocket.StatusNormalClosure, "")
}

// browserConn adapts a browser WebSocket to hub.BrowserSender + hub.BrowserCloser.
// It also carries the resolved principal so the hub's identity provider (when
// wired) can read it via UtermPrincipal. The embedded deckmux.AnonConn gives it
// a stable per-connection anonymous DeckMux identity (satisfying deckmux.Conn),
// so an anonymous browser's presence/ownership never leaks across reconnects.
type browserConn struct {
	wsBase
	deckmux.AnonConn
	principal *serverauth.Principal
	// closeGrace overrides browserCloseGrace for this connection when it is
	// non-zero. It is a field rather than a package variable a test assigns,
	// because a handler left running by an earlier test reads the grace too, and
	// a shared variable written under it is a data race.
	closeGrace time.Duration
}

// browserCloseGrace is how long a caller waits on a close before moving on.
const browserCloseGrace = 2 * time.Second

// grace is how long Close waits on this connection's peer.
func (c *browserConn) grace() time.Duration {
	if c.closeGrace > 0 {
		return c.closeGrace
	}
	return browserCloseGrace
}

// errBrowserCloseTimedOut is what Close returns when the peer has not answered
// the close handshake within the grace. The close is still under way.
var errBrowserCloseTimedOut = errors.New("browser close handshake unanswered after the grace; closing in the background")

// Close closes the browser socket with a WS close code (hub.BrowserCloser). A
// peer that does not answer the close handshake is not waited on past the
// grace: the close carries on in the background, where coder/websocket drops
// the connection once its own handshake timeout passes, so the caller (a viewer
// revoke waits on its closes) is not held by it. That case returns
// errBrowserCloseTimedOut, not a clean nil.
func (c *browserConn) Close(_ context.Context, code int, reason string) error {
	done := make(chan error, 1)
	go func() { done <- c.conn.Close(websocket.StatusCode(code), reason) }()
	select {
	case err := <-done:
		return err
	case <-time.After(c.grace()):
		return errBrowserCloseTimedOut
	}
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
