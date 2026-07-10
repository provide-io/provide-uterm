//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package hub is a Go port of the provide-uterm TermHub service classes —
// the state/services layer that arbitrates terminal-session hijack leases,
// rate-limits REST endpoints, buffers browser input, fans out events, and
// coordinates snapshot polling.
//
// This package ports "wave A": the composable service objects, NOT the
// TermHub core/router/connection orchestration nor the HTTP server (those are
// owned by a later wave). Each service is designed to be composed by that
// wave via explicit dependency injection (a shared *sync.Mutex, a
// [WorkerRegistry], a [Clock], and per-service callback interfaces).
//
// Faithfulness to the Python original:
//
//   - Lease state transitions, rate-limit token math + LRU eviction order,
//     approval expiry semantics, heartbeat/idle predicates, and polling wait
//     semantics all match the Python behaviour exactly.
//   - The Python code relies on a single-threaded asyncio event loop for
//     mutual exclusion. This port uses explicit mutexes and is safe under
//     `go test -race`.
//   - Everywhere the Python services read the clock (time.monotonic /
//     time.time) the Go services take an injectable [Clock] so tests never
//     sleep against the real wall clock.
//   - Where the Python services log via provide.telemetry get_logger, the Go
//     services accept an injectable *slog.Logger (nil selects
//     slog.Default()).
//
// Deviations from the Python original are documented at each site; the
// notable ones are: OpenTelemetry tracing spans are dropped (no OTel
// dependency), and regex compilation uses Go's RE2 engine (which rejects the
// lookaround/backreference constructs Python's `re` accepts, though the
// ReDoS-safety validator is ported verbatim).
package hub
