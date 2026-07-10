//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package client is the Go port of the provide-uterm consumer libraries
// (provide.uterm.client).
//
// It provides two building blocks that speak the same wire protocol as the
// Python client and are therefore cross-compatible with the provide-uterm
// FastAPI server:
//
//   - HijackClient — a net/http REST client for the hijack + session API. It
//     hits the same paths, verbs, JSON bodies, and query params as the Python
//     HijackClient (provide/uterm/client/hijack.py) and maps meaningful HTTP
//     status codes onto a typed *APIError.
//
//   - ControlWSClient — a github.com/coder/websocket client for the inline
//     DLE/STX control channel. It decodes interleaved terminal data and JSON
//     control frames and only ever emits framed control payloads (via
//     controlchannel.EncodeControlFrame), mirroring the repo-wide bare-JSON
//     guard.
//
// The MCP tool surface (provide/uterm/client/mcp_tools.py) is intentionally NOT
// ported here — it is a later phase.
package client
