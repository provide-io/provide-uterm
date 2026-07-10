//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"context"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/termsession"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// shellConfigKeys is the accepted connector_config key set for shell sessions.
// Deviation from Python (which only allows input_mode, being an in-memory chat
// reference): the Go shell spawns a real PTY, so it also accepts command/cols/
// rows to parameterise the spawned terminal.
var shellConfigKeys = keySet("input_mode", "command", "cols", "rows")

// newShell builds a shell connector that spawns a local shell in a PTY. When
// connector_config.command is set it is used as the argv; otherwise $SHELL (then
// /bin/sh) is spawned.
func newShell(sessionID, displayName string, config map[string]any) (*transportConnector, error) {
	if err := validateKeys(config, "shell", shellConfigKeys); err != nil {
		return nil, err
	}
	inputMode := configStr(config, "input_mode", "open")
	command := configStrList(config, "command")
	cols := configInt(config, "cols", 80)
	rows := configInt(config, "rows", 25)

	build := func() *termsession.TransportSession {
		tr := NewPTYTransport(command)
		connect := func(ctx context.Context) error {
			return tr.Connect(ctx, "", 0, transports.ConnectOptions{Cols: cols, Rows: rows})
		}
		return termsession.New(tr, connect, termsession.Options{
			Cols:         cols,
			Rows:         rows,
			SendEncoding: termsession.EncodingUTF8,
		})
	}
	return newTransportConnector(sessionID, displayName, "shell", "local shell", inputMode, build), nil
}
