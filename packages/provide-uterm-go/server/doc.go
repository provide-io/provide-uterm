//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package server is the Go port of the provide-uterm HTTP/WebSocket server
// layer (provide.uterm.server). It wires the already-ported hub, serverauth,
// serverconfig and control-plane packages into an idiomatic net/http server
// that speaks the same routes, JSON shapes, status codes and inline DLE/STX
// WebSocket wire protocol as the Python/FastAPI server — the property that
// makes a Go client interoperate with a Python server and vice versa.
//
// The public surface is [Server] + [New]. The CLI constructs a [Deps] bundle
// (hub, authenticator, authorization service, session registry, config),
// calls New, then Server.Start / Server.Shutdown.
//
// Route families:
//
//   - REST API — /api/health, /api/sessions..., /api/approvals..., /api/keys...,
//     /api/profiles..., /api/connect, /api/metrics (see the routes_*.go files).
//   - Bridge REST hijack — /worker/{id}/hijack/... + /worker/{id}/input_mode +
//     /worker/{id}/disconnect_worker (the surface the Go client.HijackClient
//     targets; see bridge_rest.go).
//   - WebSocket — /ws/worker/{id}/term (dialed by bridge.TermBridge) and
//     /ws/browser/{id}/term (dashboard viewers); see ws_worker.go / ws_browser.go.
//   - SSE — /api/sessions/{id}/events/stream (see routes_sse.go).
//
// Telemetry uses provide-telemetry (ptel.GetLogger). Configuring the telemetry
// pipeline (SetupTelemetry) is the CLI's responsibility, not this package's.
package server
