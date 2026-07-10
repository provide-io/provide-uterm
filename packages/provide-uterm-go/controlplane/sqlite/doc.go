//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package sqlite is the durable SQLite control-plane backend. Port of
// provide.uterm.control.plane.sqlite.
//
// # Driver choice: modernc.org/sqlite
//
// This package uses modernc.org/sqlite, a PURE-Go SQLite implementation, rather
// than the cgo-based mattn/go-sqlite3. Pure Go keeps cross-compilation and CI
// simple: no C toolchain, no CGO_ENABLED gymnastics, and reproducible static
// builds across every target platform. The SQLite file format is identical
// either way, so this choice does not affect the cross-compatibility guarantee.
//
// # Cross-compatibility with the Python backend
//
// The schema DDL (schema.go) is copied byte-for-byte from the Python source,
// and SQLite records each CREATE statement's exact text in sqlite_master, so a
// database created here is indistinguishable from one Python creates and vice
// versa. All columns are scalar TEXT/REAL/INTEGER (no JSON), floats round-trip
// as IEEE-754 REAL, and was_hijack_owner is stored as INTEGER 0/1 — identical
// storage classes on both sides.
//
// # Connection model
//
// Python used one aiosqlite connection serialized by an asyncio lock. Here a
// single *sql.Conn (MaxOpenConns=1) plus a Locked()-aware tx-lock reproduce
// that: BEGIN IMMEDIATE and its COMMIT run on the same connection, and
// concurrent Begin calls serialize so a lease-acquire race yields one winner.
// Every exported operation is therefore safe for concurrent use.
package sqlite
