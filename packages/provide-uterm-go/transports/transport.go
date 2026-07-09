//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package transports is a Go port of the provide-uterm client transport layer
// (packages/provide-uterm-client/src/provide/uterm/transports). It provides the
// ConnectionTransport interface and concrete telnet, WebSocket and SSH clients,
// plus reconnecting and chaos-injecting wrappers.
package transports

import (
	"context"
	"errors"
	"net/http"
	"time"
)

// Common transport defaults, mirroring the Python TelnetTransport constructor
// defaults (cols=80, rows=25, term="ANSI") and the 30s connect timeout.
const (
	// DefaultCols is the fallback terminal column count.
	DefaultCols = 80
	// DefaultRows is the fallback terminal row count.
	DefaultRows = 25
	// DefaultTerm is the fallback terminal type string.
	DefaultTerm = "ANSI"
	// DefaultConnectTimeout is the fallback connection timeout.
	DefaultConnectTimeout = 30 * time.Second
)

// Sentinel errors returned by transports. Callers may test with errors.Is.
var (
	// ErrNotConnected is returned by Send/Receive when there is no live
	// connection (mirrors Python's ConnectionError("Not connected")).
	ErrNotConnected = errors.New("not connected")
	// ErrConnectionClosed is returned when the remote closes the connection
	// (mirrors Python's ConnectionError("Connection closed by remote")).
	ErrConnectionClosed = errors.New("connection closed by remote")
)

// WSOptions carries WebSocket-specific connect options. It mirrors the kwargs
// threaded through the Python websocket transport (url, origin, additional
// headers) plus the ping/size knobs.
type WSOptions struct {
	// URL, when set, wins over host:port and is dialed verbatim. Otherwise
	// the transport builds wss://host:port.
	URL string
	// Origin sets the Origin header so a worker gating cross-origin upgrades
	// (the 4403 path) accepts the handshake.
	Origin string
	// Headers are additional HTTP headers threaded into the handshake.
	Headers http.Header
	// SendBinary forces terminal bytes onto BINARY frames. The default
	// (false) mirrors the Python transport, which forces TEXT frames so the
	// text-based Cloudflare Worker does not silently drop the payload.
	SendBinary bool
}

// SSHKeyAuth holds a private key (PEM) and optional passphrase for key auth.
type SSHKeyAuth struct {
	// PrivateKeyPEM is the PEM-encoded private key bytes.
	PrivateKeyPEM []byte
	// Passphrase decrypts an encrypted private key, when non-empty.
	Passphrase []byte
}

// SSHOptions carries SSH-specific connect options.
type SSHOptions struct {
	// User is the SSH login user.
	User string
	// Password enables password auth when non-empty.
	Password string
	// Key enables public-key auth when PrivateKeyPEM is non-empty.
	Key SSHKeyAuth
	// KnownHostsFiles are OpenSSH known_hosts files used for host-key
	// verification. When empty and InsecureSkipHostKeyVerify is false, Connect
	// fails closed rather than trusting an unknown host.
	KnownHostsFiles []string
	// InsecureSkipHostKeyVerify disables host-key checking entirely. It mirrors
	// the explicit opt-out posture of the Python SSH module, which refuses
	// insecure defaults unless a caller opts in.
	InsecureSkipHostKeyVerify bool
}

// ConnectOptions carries the connection parameters passed to Connect. Zero
// values for Cols/Rows/Term/Timeout are replaced with the package defaults.
type ConnectOptions struct {
	// Cols is the terminal column count (NAWS / PTY width).
	Cols int
	// Rows is the terminal row count (NAWS / PTY height).
	Rows int
	// Term is the terminal type string (e.g. "ANSI", "xterm-256color").
	Term string
	// Timeout bounds the connection attempt.
	Timeout time.Duration
	// WS holds WebSocket-specific options.
	WS WSOptions
	// SSH holds SSH-specific options.
	SSH SSHOptions
}

// withDefaults returns a copy of opts with zero-valued common fields replaced
// by the package defaults.
func (o ConnectOptions) withDefaults() ConnectOptions {
	if o.Cols == 0 {
		o.Cols = DefaultCols
	}
	if o.Rows == 0 {
		o.Rows = DefaultRows
	}
	if o.Term == "" {
		o.Term = DefaultTerm
	}
	if o.Timeout == 0 {
		o.Timeout = DefaultConnectTimeout
	}
	return o
}

// ConnectionTransport is the interface implemented by every concrete transport
// (telnet, WebSocket, SSH) and by the chaos / reconnecting wrappers, so they are
// interchangeable by callers. It is the Go port of the Python
// ConnectionTransport abstract base class.
type ConnectionTransport interface {
	// Connect establishes a connection to host:port. Returns an error if the
	// connection fails or times out.
	Connect(ctx context.Context, host string, port int, opts ConnectOptions) error
	// Disconnect closes the connection and releases resources. It is
	// idempotent — safe to call multiple times.
	Disconnect(ctx context.Context) error
	// Send transmits raw bytes with protocol-appropriate encoding/escaping.
	Send(ctx context.Context, data []byte) error
	// Receive reads up to maxBytes, waiting at most timeout. It returns an
	// empty slice (and nil error) on timeout, and ErrConnectionClosed when the
	// remote closes.
	Receive(ctx context.Context, maxBytes int, timeout time.Duration) ([]byte, error)
	// IsConnected reports whether the connection is currently active.
	IsConnected() bool
}
