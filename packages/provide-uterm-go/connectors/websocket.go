//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"fmt"
	"net/url"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/termsession"
)

// websocketConfigKeys mirrors WebSocketSessionConnector._VALID_CONFIG_KEYS.
var websocketConfigKeys = keySet("url", "input_mode", "hub_overlay", "block_private_connector_targets")

// newWebSocket builds a websocket connector over the WS transport. The url is
// required and must be ws:// or wss:// with a host, mirroring the Python
// connector's construction-time validation.
func newWebSocket(sessionID, displayName string, config map[string]any) (*transportConnector, error) {
	if err := validateKeys(config, "websocket", websocketConfigKeys); err != nil {
		return nil, err
	}
	raw := configStr(config, "url", "")
	if raw == "" {
		return nil, fmt.Errorf("websocket connector requires connector_config.url")
	}
	parsed, err := url.Parse(raw)
	if err != nil {
		return nil, fmt.Errorf("websocket connector_config.url is invalid: %w", err)
	}
	if parsed.Scheme != "ws" && parsed.Scheme != "wss" {
		return nil, fmt.Errorf("websocket connector_config.url scheme must be ws or wss")
	}
	if parsed.Hostname() == "" {
		return nil, fmt.Errorf("websocket connector_config.url must include a host")
	}
	inputMode := configStr(config, "input_mode", "open")

	build := func() *termsession.TransportSession {
		return termsession.NewWSSession(raw, termsession.WSOptions{})
	}
	return newTransportConnector(sessionID, displayName, "websocket", raw, inputMode, build), nil
}
