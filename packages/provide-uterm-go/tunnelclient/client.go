//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnelclient

import (
	"context"
	"errors"
	"net/http"
	"sync"
	"time"

	"github.com/coder/websocket"
)

// ErrNotConnected is returned by send/recv methods before Connect succeeds.
var ErrNotConnected = errors.New("tunnelclient: not connected")

// backoffSchedule mirrors Python's BACKOFF_SCHEDULE (seconds).
var backoffSchedule = [...]time.Duration{
	1 * time.Second, 2 * time.Second, 5 * time.Second, 10 * time.Second, 30 * time.Second,
}

// Client is an async WebSocket tunnel client. It connects to a tunnel endpoint
// and sends/receives binary tunnel frames. It is the Go port of
// provide.uterm.tunnel.client.TunnelClient.
//
// Concurrency: a single reader goroutine may call Recv while any number of
// goroutines call the Send* methods; writes are serialized by writeMu. The
// coder/websocket Conn supports one concurrent reader and one concurrent
// writer, which this mutex discipline guarantees.
type Client struct {
	wsURL string
	token string

	mu      sync.Mutex // guards conn
	conn    *websocket.Conn
	writeMu sync.Mutex // serializes all writes on conn
}

// NewClient builds a client for wsURL, authenticating with a bearer token.
func NewClient(wsURL, token string) *Client {
	return &Client{wsURL: wsURL, token: token}
}

// Connected reports whether the client currently holds an open connection.
func (c *Client) Connected() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.conn != nil
}

// Connect establishes the WebSocket connection, sending the bearer token in the
// Authorization header exactly as Python's connect() does.
func (c *Client) Connect(ctx context.Context) error {
	header := http.Header{}
	if c.token != "" {
		header.Set("Authorization", "Bearer "+c.token)
	}
	conn, resp, err := websocket.Dial(ctx, c.wsURL, &websocket.DialOptions{HTTPHeader: header})
	if resp != nil && resp.Body != nil {
		_ = resp.Body.Close()
	}
	if err != nil {
		return err
	}
	// Tunnel frames (terminal output, HTTP bodies) can exceed the 32 KiB
	// default read cap; disable it as the transports client does.
	conn.SetReadLimit(-1)
	c.mu.Lock()
	c.conn = conn
	c.mu.Unlock()
	return nil
}

// Close closes the connection. Idempotent — safe to call when never connected.
func (c *Client) Close() error {
	c.mu.Lock()
	conn := c.conn
	c.conn = nil
	c.mu.Unlock()
	if conn == nil {
		return nil
	}
	return conn.Close(websocket.StatusNormalClosure, "")
}

// current returns the live connection or ErrNotConnected.
func (c *Client) current() (*websocket.Conn, error) {
	c.mu.Lock()
	conn := c.conn
	c.mu.Unlock()
	if conn == nil {
		return nil, ErrNotConnected
	}
	return conn, nil
}

// SendRaw writes an already-framed binary payload. All other Send* helpers
// funnel through here so writes stay serialized.
func (c *Client) SendRaw(ctx context.Context, data []byte) error {
	conn, err := c.current()
	if err != nil {
		return err
	}
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	return conn.Write(ctx, websocket.MessageBinary, data)
}

// SendData sends a data frame on the given channel.
func (c *Client) SendData(ctx context.Context, data []byte, channel byte) error {
	return c.SendRaw(ctx, EncodeFrame(channel, data, FlagData))
}

// SendEOF sends an EOF frame on the given channel.
func (c *Client) SendEOF(ctx context.Context, channel byte) error {
	return c.SendRaw(ctx, EncodeFrame(channel, nil, FlagEOF))
}

// SendControl encodes and sends a control message on the control channel.
func (c *Client) SendControl(ctx context.Context, msg map[string]any) error {
	frame, err := EncodeControl(msg)
	if err != nil {
		return err
	}
	return c.SendRaw(ctx, frame)
}

// OpenTerminal sends the control message that opens a terminal channel.
func (c *Client) OpenTerminal(ctx context.Context, cols, rows int) error {
	return c.SendRaw(ctx, OpenTerminalFrame(cols, rows))
}

// SendResize sends a terminal-resize control message.
func (c *Client) SendResize(ctx context.Context, cols, rows int) error {
	return c.SendRaw(ctx, ResizeFrame(cols, rows))
}

// RecvRaw returns the next WebSocket message's raw bytes without decoding. The
// share bridge writes these straight to the PTY, matching share.py which does
// not strip the frame header on the inbound path.
func (c *Client) RecvRaw(ctx context.Context) ([]byte, error) {
	conn, err := c.current()
	if err != nil {
		return nil, err
	}
	_, data, rerr := conn.Read(ctx)
	return data, rerr
}

// RecvMessage returns the next WebSocket message's raw bytes and whether it was
// a text frame. The inspect action receiver needs this distinction because the
// server relays browser actions either as binary tunnel frames (ChannelHTTP) or
// as bare text JSON.
func (c *Client) RecvMessage(ctx context.Context) (isText bool, data []byte, err error) {
	conn, cerr := c.current()
	if cerr != nil {
		return false, nil, cerr
	}
	typ, data, rerr := conn.Read(ctx)
	return typ == websocket.MessageText, data, rerr
}

// Recv receives and decodes the next tunnel frame. A text message is decoded
// from its raw bytes just like the binary path (matching Python, which treats
// str frames as latin-1 bytes before decode_frame).
func (c *Client) Recv(ctx context.Context) (Frame, error) {
	conn, err := c.current()
	if err != nil {
		return Frame{}, err
	}
	_, data, rerr := conn.Read(ctx)
	if rerr != nil {
		return Frame{}, rerr
	}
	return DecodeFrame(data)
}

// ReconnectLoop attempts to (re)connect with the Python backoff schedule.
// maxAttempts of 0 means unlimited. It returns nil on the first successful
// connect, or ctx.Err() if the context is cancelled while waiting.
func (c *Client) ReconnectLoop(ctx context.Context, maxAttempts int) error {
	limit := maxAttempts
	if limit == 0 {
		limit = 1 << 31
	}
	for attempt := 0; attempt < limit; attempt++ {
		idx := attempt
		if idx >= len(backoffSchedule) {
			idx = len(backoffSchedule) - 1
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoffSchedule[idx]):
		}
		if err := c.Connect(ctx); err == nil {
			return nil
		}
	}
	return ErrNotConnected
}
