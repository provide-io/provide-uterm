//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package termsession

import (
	"context"
	"net/http"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// TelnetOptions configure ConnectTelnet.
type TelnetOptions struct {
	// Cols/Rows default to 80×25 (also sent via NAWS).
	Cols, Rows int
	// Term is the TTYPE terminal string; "" selects "ANSI".
	Term string
	// ConnectTimeout defaults to 30s.
	ConnectTimeout time.Duration
	// ControlFrames enables inline DLE/STX control-frame parsing (see
	// Options.ControlFrames). Off by default.
	ControlFrames bool
}

// NewTelnetSession builds a telnet-backed TransportSession (full RFC 854 IAC
// negotiation with NAWS/TTYPE — required by TWGS and other BBS servers).
// CP437 send encoding preserves the high-byte/ANSI conventions BBS servers
// expect. Call Connect to dial. Port of telnet_session.TelnetSession.
func NewTelnetSession(host string, port int, opts TelnetOptions) *TransportSession {
	if opts.Cols <= 0 {
		opts.Cols = 80
	}
	if opts.Rows <= 0 {
		opts.Rows = 25
	}
	if opts.Term == "" {
		opts.Term = "ANSI"
	}
	if opts.ConnectTimeout == 0 {
		opts.ConnectTimeout = 30 * time.Second
	}
	transport := transports.NewTelnetTransport()
	connect := func(ctx context.Context) error {
		return transport.Connect(ctx, host, port, transports.ConnectOptions{
			Cols:    opts.Cols,
			Rows:    opts.Rows,
			Term:    opts.Term,
			Timeout: opts.ConnectTimeout,
		})
	}
	return New(transport, connect, Options{
		Cols: opts.Cols, Rows: opts.Rows, SendEncoding: EncodingCP437, ControlFrames: opts.ControlFrames,
	})
}

// ConnectTelnet dials a telnet server and returns a connected session. Port
// of telnet_session.connect_telnet.
func ConnectTelnet(ctx context.Context, host string, port int, opts TelnetOptions) (*TransportSession, error) {
	s := NewTelnetSession(host, port, opts)
	if err := s.Connect(ctx); err != nil {
		return nil, err
	}
	return s, nil
}

// WSOptions configure ConnectWS.
type WSOptions struct {
	// Cols/Rows default to 80×25.
	Cols, Rows int
	// Origin, when non-empty, is sent on the upgrade so workers that gate
	// cross-origin WS upgrades (the 4403 path) see an allowed Origin.
	Origin string
	// AdditionalHeaders are extra upgrade headers.
	AdditionalHeaders http.Header
	// ControlFrames enables inline DLE/STX control-frame parsing (see
	// Options.ControlFrames). Off by default.
	ControlFrames bool
}

// NewWSSession builds a WebSocket-backed TransportSession (UTF-8 send
// encoding). Call Connect to dial. Port of ws_session.WebSocketSession —
// keepalive tuning (the Python ping_interval/ping_timeout knobs) is handled
// inside coder/websocket and is not exposed here.
func NewWSSession(url string, opts WSOptions) *TransportSession {
	if opts.Cols <= 0 {
		opts.Cols = 80
	}
	if opts.Rows <= 0 {
		opts.Rows = 25
	}
	transport := transports.NewWebSocketTransport()
	connect := func(ctx context.Context) error {
		return transport.Connect(ctx, "", 0, transports.ConnectOptions{
			Cols: opts.Cols,
			Rows: opts.Rows,
			WS: transports.WSOptions{
				URL:     url,
				Origin:  opts.Origin,
				Headers: opts.AdditionalHeaders,
			},
		})
	}
	return New(transport, connect, Options{
		Cols: opts.Cols, Rows: opts.Rows, SendEncoding: EncodingUTF8, ControlFrames: opts.ControlFrames,
	})
}

// ConnectWS dials a WebSocket server and returns a connected session. Port
// of ws_session.connect_ws.
func ConnectWS(ctx context.Context, url string, opts WSOptions) (*TransportSession, error) {
	s := NewWSSession(url, opts)
	if err := s.Connect(ctx); err != nil {
		return nil, err
	}
	return s, nil
}
