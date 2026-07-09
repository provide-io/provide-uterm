//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/coder/websocket"
	ptel "github.com/provide-io/provide-telemetry/go"
)

// WebSocketTransport is a WebSocket client implementing ConnectionTransport,
// ported from the Python WebSocketTransport (ws_transport.py) and the URL /
// origin / additional-header threading from websocket.py.
//
// It uses github.com/coder/websocket (the actively-maintained successor of
// nhooyr/websocket) for its context-first API and leak-free goroutine model.
//
// Deviation from Python: coder/websocket closes the connection if a Read's
// context expires (the WebSocket wire protocol cannot resume a partial read).
// To preserve Python's "empty slice on timeout, connection stays open" contract,
// this transport runs a background reader goroutine that reads whole messages
// with a long-lived context; Receive selects that channel against the timeout.
type WebSocketTransport struct {
	mu        sync.Mutex
	conn      *websocket.Conn
	url       string
	connected bool
	binary    bool

	baseCancel context.CancelFunc
	rxCh       chan []byte
	closed     chan struct{}
}

// NewWebSocketTransport returns an unconnected WebSocketTransport.
func NewWebSocketTransport() *WebSocketTransport {
	return &WebSocketTransport{}
}

// Connect dials the WebSocket. When opts.WS.URL is set it wins; otherwise the
// transport builds wss://host:port. Origin and additional headers are threaded
// into the handshake so a worker gating cross-origin upgrades accepts it.
func (t *WebSocketTransport) Connect(ctx context.Context, host string, port int, opts ConnectOptions) error {
	logger := ptel.GetLogger(ctx, "provide.uterm.transports.websocket")
	opts = opts.withDefaults()

	url := opts.WS.URL
	if url == "" {
		url = "wss://" + net.JoinHostPort(host, strconv.Itoa(port))
	}

	header := http.Header{}
	for k, v := range opts.WS.Headers {
		header[k] = append([]string(nil), v...)
	}
	if opts.WS.Origin != "" {
		header.Set("Origin", opts.WS.Origin)
	}

	dialCtx, cancel := context.WithTimeout(ctx, opts.Timeout)
	defer cancel()
	conn, _, err := websocket.Dial(dialCtx, url, &websocket.DialOptions{HTTPHeader: header})
	if err != nil {
		return fmt.Errorf("failed to connect to %s: %w", url, err)
	}
	conn.SetReadLimit(-1) // terminal frames can be large; disable the 32 KiB cap.

	baseCtx, baseCancel := context.WithCancel(context.Background())
	t.mu.Lock()
	t.conn = conn
	t.url = url
	t.connected = true
	t.binary = opts.WS.SendBinary
	t.baseCancel = baseCancel
	t.rxCh = make(chan []byte)
	t.closed = make(chan struct{})
	rxCh, closed := t.rxCh, t.closed
	t.mu.Unlock()

	go t.readLoop(baseCtx, conn, rxCh, closed)

	logger.Debug("WebSocketTransport connected", "url", url)
	return nil
}

// readLoop reads whole messages until the connection closes or baseCtx cancels.
func (t *WebSocketTransport) readLoop(baseCtx context.Context, conn *websocket.Conn, rxCh chan []byte, closed chan struct{}) {
	defer close(closed)
	for {
		_, data, err := conn.Read(baseCtx)
		if err != nil {
			return
		}
		select {
		case rxCh <- data:
		case <-baseCtx.Done():
			return
		}
	}
}

// Disconnect closes the connection. Idempotent.
func (t *WebSocketTransport) Disconnect(ctx context.Context) error {
	t.mu.Lock()
	conn := t.conn
	cancel := t.baseCancel
	t.conn = nil
	t.baseCancel = nil
	t.connected = false
	t.mu.Unlock()

	if cancel != nil {
		cancel()
	}
	if conn != nil {
		_ = conn.Close(websocket.StatusNormalClosure, "")
		ptel.GetLogger(ctx, "provide.uterm.transports.websocket").Debug("WebSocketTransport disconnected")
	}
	return nil
}

// Send transmits data. By default it uses a TEXT frame (mirroring Python, whose
// text-based Cloudflare Worker silently drops BINARY frames); set
// WSOptions.SendBinary to force BINARY.
func (t *WebSocketTransport) Send(ctx context.Context, data []byte) error {
	t.mu.Lock()
	conn := t.conn
	connected := t.connected
	binary := t.binary
	t.mu.Unlock()
	if !connected || conn == nil {
		return fmt.Errorf("%w: websocket send", ErrNotConnected)
	}

	msgType := websocket.MessageText
	if binary {
		msgType = websocket.MessageBinary
	}
	if err := conn.Write(ctx, msgType, data); err != nil {
		_ = t.Disconnect(ctx)
		return fmt.Errorf("%w: %v", ErrConnectionClosed, err)
	}
	return nil
}

// Receive returns the next whole message, an empty slice on timeout, or
// ErrConnectionClosed when the connection drops. maxBytes is advisory and
// ignored — WebSocket is message-framed, so each read yields one whole message.
func (t *WebSocketTransport) Receive(ctx context.Context, maxBytes int, timeout time.Duration) ([]byte, error) {
	_ = maxBytes
	t.mu.Lock()
	connected := t.connected
	rxCh := t.rxCh
	closed := t.closed
	t.mu.Unlock()
	if !connected || rxCh == nil {
		return nil, fmt.Errorf("%w: websocket receive", ErrNotConnected)
	}

	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case msg := <-rxCh:
		return msg, nil
	case <-timer.C:
		return []byte{}, nil
	case <-closed:
		_ = t.Disconnect(ctx)
		return nil, ErrConnectionClosed
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// IsConnected reports whether the connection is active.
func (t *WebSocketTransport) IsConnected() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.connected && t.conn != nil
}

// Compile-time assertion that WebSocketTransport implements ConnectionTransport.
var _ ConnectionTransport = (*WebSocketTransport)(nil)
