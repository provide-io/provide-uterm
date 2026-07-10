//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"fmt"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/termsession"
)

// telnetConfigKeys mirrors TelnetSessionConnector._VALID_CONFIG_KEYS.
var telnetConfigKeys = keySet("host", "port", "input_mode", "hub_overlay", "block_private_connector_targets")

// newTelnet builds a telnet connector over the telnet transport. Defaults match
// the Python connector: host TerminalDefaults.TELNET_HOST, port TELNET_REMOTE_PORT (23).
func newTelnet(sessionID, displayName string, config map[string]any) (*transportConnector, error) {
	if err := validateKeys(config, "telnet", telnetConfigKeys); err != nil {
		return nil, err
	}
	host := configStr(config, "host", defaults.TelnetHost)
	port := configInt(config, "port", defaults.TelnetRemotePort)
	inputMode := configStr(config, "input_mode", "open")

	build := func() *termsession.TransportSession {
		return termsession.NewTelnetSession(host, port, termsession.TelnetOptions{})
	}
	upstream := fmt.Sprintf("telnet://%s:%d", host, port)
	return newTransportConnector(sessionID, displayName, "telnet", upstream, inputMode, build), nil
}
