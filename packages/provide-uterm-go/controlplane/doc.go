//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package controlplane is a behavior-faithful Go port of the provide-uterm
// control-plane storage layer (Python: provide.uterm.control.plane).
//
// The control plane persists the durable state that survives worker restarts:
// sessions, session/resume tokens, command approvals, hijack leases, and the
// audit-chain head. Two interchangeable backends implement the Engine
// interface with identical observable semantics:
//
//   - controlplane/memory — volatile, stdlib-only, snapshot-isolated
//     transactions with optimistic-concurrency conflict detection.
//   - controlplane/sqlite — durable SQLite file; its schema, migrations and
//     query shapes are byte-compatible with the Python backend so a database
//     created by Python is readable by Go and vice versa.
//
// controlplane/bootstrap wires a Config to the matching backend (Python
// bootstrap_control_plane).
//
// This top-level package holds only the shared vocabulary — record structs,
// Config/EngineCapabilities, the error taxonomy, and the Engine/Tx/store
// interfaces — and pulls in no third-party dependencies (stdlib database/sql
// Scanner/Valuer plumbing only). The SQLite driver lives behind the sqlite
// subpackage so memory-only consumers stay dependency-light.
//
// # Differences from Python
//
//   - The Python API is async (asyncio); this port is synchronous and takes a
//     context.Context first argument per Go convention. The stored bytes and
//     ordering guarantees are unchanged.
//   - Nullable columns use the comparable NullString/NullFloat value types
//     rather than *string/*float64 so records compare by value with ==, which
//     the memory backend's conflict detection depends on (Python compares
//     frozen-dataclass values).
//   - Every exported type is safe for concurrent use (Python relied on the
//     single-threaded event loop).
//
// # Schema is not JSON
//
// The Python backend stores only scalar TEXT/REAL/INTEGER columns — there are
// no JSON blob columns — so cross-compatibility is a matter of matching the
// SQL schema and storage classes, not a JSON wire shape.
//
// # control/channel is not ported here
//
// Python's control.channel subpackage is two zero-logic dataclasses
// (BrowserControlMessage/WorkerControlMessage: a type string + a payload map)
// with no encode/decode behavior. The actual inline DLE/STX control-frame wire
// format lives in the existing controlchannel and ctrlmsg Go packages, so there
// is nothing storage-related to port from control/channel.
package controlplane
