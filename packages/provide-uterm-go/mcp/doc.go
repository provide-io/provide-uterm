//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package mcp is the Go port of the provide-uterm AI/MCP tool surface
// (provide.uterm.ai / provide.uterm.client.mcp_tools).
//
// It exposes the same 21 Model Context Protocol tools as the Python
// "uterm-mcp" server — session management, hijack lifecycle, real-time event
// subscription, fan-out, annotation, and server/worker control — with byte-for-
// byte identical tool names, argument validation, and error strings so an MCP
// client configuration is interchangeable between the Python and Go servers.
//
// Every tool funnels caller/LLM-supplied input through the same input-hardening
// validators as the reference implementation (SSRF host classification,
// ReDoS-guarded regex compilation, path-segment id validation, connector-config
// vetting, snapshot shaping) before it reaches the REST/WS API via the ported
// client.HijackClient, and every handler is gated by the authorization
// chokepoint (see policy.go / auth.go).
//
// Tool registration uses github.com/mark3labs/mcp-go — the de-facto standard Go
// MCP SDK — which owns the JSON-RPC framing, tool schema advertisement, and the
// stdio/SSE transports. CreateServer wires the tools onto a *server.MCPServer;
// the cmd/uterm-mcp binary runs it over stdio, matching the Python CLI.
package mcp
