//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package defaults provides default host/port constants for provide-uterm
// transports.
//
// It is a port of the Python module provide.uterm.defaults
// (packages/provide-uterm/src/provide/uterm/defaults.py, class
// TerminalDefaults). Override at the call site rather than shadowing these
// values.
package defaults

import (
	"os"
	"path/filepath"
)

const (
	// TelnetHost is the default telnet host.
	TelnetHost = "127.0.0.1"
	// TelnetPort is the default telnet port.
	TelnetPort = 2102
	// SSHPort is the default SSH port.
	SSHPort = 2222
	// GatewayTelnetPort is the default gateway telnet port.
	GatewayTelnetPort = 2112
	// GatewaySSHPort is the default gateway SSH port.
	GatewaySSHPort = 2222

	// BindAll is the bind-all address for gateway/proxy listeners.
	BindAll = "0.0.0.0"
	// ProxyPort is the uterm proxy default HTTP listen port.
	ProxyPort = 8765
	// ProxyWSPath is the uterm proxy default WebSocket path.
	ProxyWSPath = "/ws/terminal"
	// ProxyPollMS is the interval in milliseconds between remote-receive polls
	// in the WS→transport proxy. Mirrors WsTerminalProxy._POLL_MS in Python
	// (packages/provide-uterm-server/.../fastapi_utils.py).
	ProxyPollMS = 50
	// ServerHost is the provide-uterm-server default bind host.
	ServerHost = "127.0.0.1"
	// ServerPort is the provide-uterm-server default port.
	ServerPort = 8780
	// TelnetRemotePort is the default remote telnet port (connect-to).
	TelnetRemotePort = 23
	// SSHRemotePort is the default remote SSH port (connect-to).
	SSHRemotePort = 22
	// WSPingInterval is the websocket ping interval (seconds).
	WSPingInterval = 20
	// WSPingTimeout is the websocket ping response timeout (seconds).
	WSPingTimeout = 20
	// WSCloseTimeout is the websocket close timeout (seconds).
	WSCloseTimeout = 10
	// ReconnectMaxRetries is the number of reconnect attempts before giving up.
	ReconnectMaxRetries = 5
	// ReconnectBaseBackoffS is the initial reconnect backoff (seconds).
	ReconnectBaseBackoffS = 0.5
	// ReconnectMaxBackoffS is the ceiling for reconnect backoff (seconds).
	ReconnectMaxBackoffS = 30.0
)

// TokenFile returns the default resume-token file path
// (~/.uterm/session_token). Port of TerminalDefaults.token_file().
func TokenFile() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".uterm", "session_token"), nil
}
