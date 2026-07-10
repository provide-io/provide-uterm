//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnelclient

import "encoding/json"

// The control-message builders below marshal Go structs whose fields are
// declared in the SAME order as the Python dict literals in client.py /
// cli/*.py. Go's encoding/json emits struct fields in declaration order with
// no inter-token whitespace, so the bytes are identical to Python's
// json.dumps(msg, separators=(",", ":")). This is what makes control frames
// wire-parity-faithful, not merely semantically equivalent.

type openTerminalMsg struct {
	Type       string `json:"type"`
	Channel    int    `json:"channel"`
	TunnelType string `json:"tunnel_type"`
	TermSize   [2]int `json:"term_size"`
}

type resizeMsg struct {
	Type    string `json:"type"`
	Channel int    `json:"channel"`
	Cols    int    `json:"cols"`
	Rows    int    `json:"rows"`
}

type openPortMsg struct {
	Type       string `json:"type"`
	Channel    int    `json:"channel"`
	TunnelType string `json:"tunnel_type"`
	LocalPort  int    `json:"local_port"`
}

// mustMarshal marshals v; control structs above cannot fail to marshal, so a
// panic here would indicate a programming error, never runtime input.
func mustMarshal(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil { // pragma: no cover — fixed structs never fail
		panic(err)
	}
	return b
}

// OpenTerminalFrame builds the control frame that opens a terminal channel,
// mirroring TunnelClient.open_terminal.
func OpenTerminalFrame(cols, rows int) []byte {
	return EncodeControlBytes(mustMarshal(openTerminalMsg{
		Type: "open", Channel: 1, TunnelType: "terminal", TermSize: [2]int{cols, rows},
	}))
}

// ResizeFrame builds the terminal-resize control frame (send_resize).
func ResizeFrame(cols, rows int) []byte {
	return EncodeControlBytes(mustMarshal(resizeMsg{
		Type: "resize", Channel: 1, Cols: cols, Rows: rows,
	}))
}

// OpenTCPFrame builds the control frame that opens a TCP relay channel
// (cli/tunnel.py's open message on ChannelTCP).
func OpenTCPFrame(localPort int) []byte {
	return EncodeControlBytes(mustMarshal(openPortMsg{
		Type: "open", Channel: int(ChannelTCP), TunnelType: "tcp", LocalPort: localPort,
	}))
}

// OpenHTTPFrame builds the control frame that opens an HTTP inspection channel
// (cli/inspect.py's open message on ChannelHTTP).
func OpenHTTPFrame(localPort int) []byte {
	return EncodeControlBytes(mustMarshal(openPortMsg{
		Type: "open", Channel: int(ChannelHTTP), TunnelType: "http", LocalPort: localPort,
	}))
}
