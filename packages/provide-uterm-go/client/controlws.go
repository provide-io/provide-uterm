//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package client

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"

	"github.com/coder/websocket"
	ptel "github.com/provide-io/provide-telemetry/go"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// WSRole is the perspective of a control-channel WebSocket endpoint. It decides
// whether decoded terminal data is surfaced as an "input" frame (worker side)
// or a "term" frame (browser side).
type WSRole string

const (
	// RoleBrowser is the browser/observer side; decoded data → "term" frames.
	RoleBrowser WSRole = "browser"
	// RoleWorker is the worker side; decoded data → "input" frames.
	RoleWorker WSRole = "worker"
)

// inferRole mirrors the Python role inference: a URL containing "/ws/worker/"
// is a worker, everything else is a browser.
func inferRole(url string) WSRole {
	if strings.Contains(url, "/ws/worker/") {
		return RoleWorker
	}
	return RoleBrowser
}

// toStr renders a control-frame "data" field as a string, matching Python's
// str(payload.get("data", "")).
func toStr(v any) string {
	switch s := v.(type) {
	case nil:
		return ""
	case string:
		return s
	default:
		return fmt.Sprint(s)
	}
}

// encodeLogicalFrame encodes one logical terminal/control frame for the inline
// WS protocol. Frames of type "input" or "term" go through the terminal-data
// channel; every other frame is a DLE/STX control frame. Port of
// encode_logical_frame in control_ws.py.
func encodeLogicalFrame(payload map[string]any) (string, error) {
	if t, ok := payload["type"].(string); ok && (t == "input" || t == "term") {
		return controlchannel.EncodeTerminalData(toStr(payload["data"])), nil
	}
	return controlchannel.EncodeControlFrame(payload)
}

// LogicalFrameDecoder incrementally maps inline DLE/STX chunks back to logical
// WS frames (control payloads, or {"type": <data-type>, "data": ...} for
// terminal data). Port of LogicalFrameDecoder in control_ws.py.
type LogicalFrameDecoder struct {
	role    WSRole
	decoder *controlchannel.Decoder
}

// NewLogicalFrameDecoder returns a decoder for the given role.
func NewLogicalFrameDecoder(role WSRole) *LogicalFrameDecoder {
	return &LogicalFrameDecoder{role: role, decoder: controlchannel.NewDecoder(controlchannel.DecoderOptions{})}
}

func (d *LogicalFrameDecoder) dataType() string {
	if d.role == RoleWorker {
		return "input"
	}
	return "term"
}

func (d *LogicalFrameDecoder) mapChunks(chunks []controlchannel.Chunk) []map[string]any {
	frames := make([]map[string]any, 0, len(chunks))
	for _, chunk := range chunks {
		switch ch := chunk.(type) {
		case controlchannel.ControlChunk:
			frames = append(frames, ch.Control)
		case controlchannel.DataChunk:
			frames = append(frames, map[string]any{"type": d.dataType(), "data": ch.Data})
		}
	}
	return frames
}

// Feed decodes all complete logical frames from raw and buffers the rest.
func (d *LogicalFrameDecoder) Feed(raw string) ([]map[string]any, error) {
	chunks, err := d.decoder.Feed(raw)
	if err != nil {
		return nil, err
	}
	return d.mapChunks(chunks), nil
}

// Finish decodes any remaining buffered data, rejecting truncated frames.
func (d *LogicalFrameDecoder) Finish() ([]map[string]any, error) {
	chunks, err := d.decoder.Finish()
	if err != nil {
		return nil, err
	}
	return d.mapChunks(chunks), nil
}

// ControlWSClient is a concurrency-safe, codec-aware WebSocket client for the
// inline control channel. It is the Go analogue of
// AsyncInlineWebSocketClient: SendFrame/SendJSON only ever emit framed
// payloads (terminal data or DLE/STX control frames), never bare JSON, and
// RecvFrame decodes interleaved data and control chunks in FIFO order.
//
// A ControlWSClient is safe for one concurrent reader (RecvFrame/ReceiveJSON)
// and one concurrent writer (SendFrame/SendJSON/Send) — the coder/websocket
// concurrency model — guarded by independent read/write mutexes.
type ControlWSClient struct {
	conn     *websocket.Conn
	decoder  *LogicalFrameDecoder
	ownsConn bool

	writeMu sync.Mutex

	readMu  sync.Mutex
	pending []map[string]any
}

// NewControlWSClient wraps an already-connected coder/websocket connection
// (e.g. one obtained from websocket.Accept on the server side, or a caller's
// own Dial) with the inline control-channel codec. The caller retains
// ownership of conn and is responsible for closing it.
func NewControlWSClient(conn *websocket.Conn, role WSRole) *ControlWSClient {
	return &ControlWSClient{conn: conn, decoder: NewLogicalFrameDecoder(role)}
}

// DialOptions configure Dial. Role overrides the URL-based role inference when
// set; Headers are threaded into the WebSocket handshake.
type DialOptions struct {
	Role    WSRole
	Headers http.Header
}

// Dial connects to a control-channel WebSocket at url and wraps it. The
// returned client owns the connection and closes it in Close. Port of
// connect_async_ws in control_ws.py.
func Dial(ctx context.Context, url string, opts *DialOptions) (*ControlWSClient, error) {
	logger := ptel.GetLogger(ctx, "provide.uterm.client.control_ws")

	role := inferRole(url)
	var header http.Header
	if opts != nil {
		if opts.Role != "" {
			role = opts.Role
		}
		header = opts.Headers
	}

	conn, _, err := websocket.Dial(ctx, url, &websocket.DialOptions{HTTPHeader: header})
	if err != nil {
		logger.Warn("ControlWSClient dial failed", "url", url, "error", err.Error())
		return nil, fmt.Errorf("control-ws dial %s: %w", url, err)
	}
	conn.SetReadLimit(-1) // control/terminal frames can be large.

	c := NewControlWSClient(conn, role)
	c.ownsConn = true
	return c, nil
}

// SendFrame encodes payload with the inline protocol and writes it as a single
// text WebSocket message.
func (c *ControlWSClient) SendFrame(ctx context.Context, payload map[string]any) error {
	encoded, err := encodeLogicalFrame(payload)
	if err != nil {
		return err
	}
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	return c.conn.Write(ctx, websocket.MessageText, []byte(encoded))
}

// SendJSON is the named parity method for send_json: it requires a mapping
// payload and routes it through SendFrame so control payloads stay framed.
func (c *ControlWSClient) SendJSON(ctx context.Context, payload map[string]any) error {
	return c.SendFrame(ctx, payload)
}

// Send mirrors the string branch of the Python async send(): a JSON-object
// string is rejected (callers must use SendFrame/SendJSON so control stays
// framed); any other string is written verbatim as a text message.
func (c *ControlWSClient) Send(ctx context.Context, data string) error {
	var parsed any
	if err := json.Unmarshal([]byte(data), &parsed); err == nil {
		if _, isObj := parsed.(map[string]any); isObj {
			return fmt.Errorf("bare JSON control strings are not accepted; use SendFrame() or SendJSON()")
		}
	}
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	return c.conn.Write(ctx, websocket.MessageText, []byte(data))
}

// RecvFrame returns the next logical frame, decoding interleaved control and
// terminal-data chunks in FIFO order. It reads more WebSocket messages only
// when the pending buffer is empty. Non-text messages are rejected, matching
// the Python client's text-only recv contract.
func (c *ControlWSClient) RecvFrame(ctx context.Context) (map[string]any, error) {
	c.readMu.Lock()
	defer c.readMu.Unlock()
	for {
		if len(c.pending) > 0 {
			frame := c.pending[0]
			c.pending = c.pending[1:]
			return frame, nil
		}
		msgType, raw, err := c.conn.Read(ctx)
		if err != nil {
			return nil, err
		}
		if msgType != websocket.MessageText {
			return nil, fmt.Errorf("expected text WebSocket payload, got %s", msgType)
		}
		frames, err := c.decoder.Feed(string(raw))
		if err != nil {
			return nil, err
		}
		c.pending = append(c.pending, frames...)
	}
}

// ReceiveJSON is the named parity method for receive_json; it delegates to
// RecvFrame.
func (c *ControlWSClient) ReceiveJSON(ctx context.Context) (map[string]any, error) {
	return c.RecvFrame(ctx)
}

// Close closes the underlying connection when this client owns it (i.e. it was
// created via Dial). For a wrapped connection it is a no-op and the caller
// remains responsible for closing.
func (c *ControlWSClient) Close(code websocket.StatusCode, reason string) error {
	if !c.ownsConn {
		return nil
	}
	return c.conn.Close(code, reason)
}
