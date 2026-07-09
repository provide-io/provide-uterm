//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package deckmux ports the provide-uterm DeckMux collaborative-presence
// subsystem (Python: provide.uterm.deckmux) to Go.
//
// DeckMux tracks the ephemeral, per-session presence of every browser
// watching a shared terminal (scroll position, selection, pin, typing
// state), arbitrates which participant holds the input "control" lease,
// buffers non-owner keystrokes, and generates deterministic display
// names/colors. It emits four wire messages that are byte-compatible
// (map-compare level) with the Python DeckMux and with the frames package
// structs PresenceUpdateFrame, PresenceSyncFrame, PresenceLeaveFrame and
// ControlTransferFrame:
//
//   - presence_update  — one user's presence changed
//   - presence_sync    — full roster (sent to a joining browser)
//   - presence_leave   — a user disconnected
//   - control_transfer — the input owner changed
//
// Unlike the asyncio (single-threaded) Python original, every exported type
// here is safe for concurrent use: PresenceStore, TransferManager and the
// DeckMuxPresence service guard their shared state with mutexes and pass
// go test -race.
//
// Layout mirrors the Python modules:
//
//	names.go     ← _names.py     deterministic name/color/initials
//	edge.go      ← _edge.py      viewport→edge-bar range math
//	protocol.go  ← _protocol.py  message constants + wire builders
//	presence.go  ← _presence.py  UserPresence + PresenceStore
//	transfer.go  ← _transfer.py  TransferManager (control transfer + queue)
//	identity.go  ← _identity.py  identity-frame parsing + principal adapter
//	service.go   ← _service.py   DeckMuxPresence routing service
package deckmux
