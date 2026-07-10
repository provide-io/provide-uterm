//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"net"
	"strings"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/colors"
)

// TelnetWsGateway is a raw-TCP (telnet) listener that proxies each inbound
// connection to a WebSocket terminal server. Direct port of the Python
// TelnetWsGateway.
type TelnetWsGateway struct {
	WSURL string
	// ColorMode is the ANSI color downgrade applied to upstream output.
	ColorMode colors.ColorMode
	// IacNegotiate enables RFC 1091 TTYPE / RFC 1572 NEW-ENVIRON negotiation to
	// derive a color palette forwarded upstream as ?colormode=.
	IacNegotiate bool
	// IacNegotiateTimeout bounds the pre-connect negotiation window.
	IacNegotiateTimeout time.Duration
	// AllowUnauthenticated permits binding a non-loopback address. Telnet is
	// plaintext and unauthenticated, so it is not silently exposed otherwise.
	AllowUnauthenticated bool
	// TLSConfig, when set, is used for wss:// upstreams.
	TLSConfig *tls.Config
	// MaxReconnects / ReconnectDelay bound the WS reconnect loop.
	MaxReconnects  int
	ReconnectDelay time.Duration
}

// Start binds host:port and returns the listener. Binding a non-loopback host
// requires AllowUnauthenticated=true (fail-closed security gate mirroring the
// Python gateway).
func (g *TelnetWsGateway) Start(host string, port int) (net.Listener, error) {
	if !g.AllowUnauthenticated && !isLoopbackBindHost(host) {
		return nil, errors.New(
			"refusing to start an unauthenticated telnet gateway on a non-loopback bind address; " +
				"set --allow-unauthenticated-telnet only when this listener is protected by another access-control layer")
	}
	return net.Listen("tcp", net.JoinHostPort(host, fmt.Sprintf("%d", port)))
}

// Serve accepts connections on ln until ctx is cancelled, handling each in its
// own goroutine.
func (g *TelnetWsGateway) Serve(ctx context.Context, ln net.Listener) error {
	go func() { <-ctx.Done(); _ = ln.Close() }()
	for {
		conn, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		go g.handleConn(ctx, conn)
	}
}

// handleConn negotiates IAC (when enabled) then drives the bidirectional pump.
func (g *TelnetWsGateway) handleConn(ctx context.Context, conn net.Conn) {
	defer conn.Close() //nolint:errcheck // best-effort close

	wsURL := g.WSURL
	var neg *IacNegotiator
	if g.IacNegotiate {
		neg = g.negotiate(conn)
		if derived := neg.DerivedColormode(); derived != "" {
			sep := "?"
			if strings.Contains(wsURL, "?") {
				sep = "&"
			}
			wsURL = fmt.Sprintf("%s%scolormode=%s", wsURL, sep, derived)
		}
	}

	readTransform := func(data []byte) (up, reply []byte) {
		if neg != nil {
			reply, up = neg.Feed(data)
			return up, reply
		}
		return stripIAC(data), nil
	}

	drive(ctx, driveParams{
		wsURL:          wsURL,
		tlsConfig:      g.TLSConfig,
		client:         conn,
		readTransform:  readTransform,
		writeTransform: telnetWriteTransform(g.ColorMode),
		showReconnect: func() {
			_, _ = conn.Write([]byte("\x1b7\x1b[999;1H\x1b[2;36m* reconnecting...\x1b[0m\x1b8"))
		},
		maxReconnects:  g.MaxReconnects,
		reconnectDelay: g.ReconnectDelay,
	})
}

// negotiate performs the pre-connect IAC handshake: it sends the DO options
// then reads client replies within the timeout window, feeding the negotiator.
// Application bytes received during this window are discarded (a client
// shouldn't send input before the welcome banner).
func (g *TelnetWsGateway) negotiate(conn net.Conn) *IacNegotiator {
	neg := NewIacNegotiator()
	_, _ = conn.Write(neg.StartBytes())
	deadline := time.Now().Add(g.IacNegotiateTimeout)
	buf := make([]byte, 4096)
	for !neg.Done() {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			break
		}
		_ = conn.SetReadDeadline(time.Now().Add(remaining))
		n, err := conn.Read(buf)
		if n > 0 {
			if reply, _ := neg.Feed(buf[:n]); len(reply) > 0 {
				_, _ = conn.Write(reply)
			}
		}
		if err != nil {
			break
		}
	}
	_ = conn.SetReadDeadline(time.Time{})
	return neg
}

// isLoopbackBindHost reports whether host is a loopback bind address. Mirrors
// _is_loopback_bind_host.
func isLoopbackBindHost(host string) bool {
	n := strings.ToLower(strings.Trim(strings.TrimSpace(host), "[]"))
	if n == "localhost" {
		return true
	}
	ip := net.ParseIP(n)
	return ip != nil && ip.IsLoopback()
}
