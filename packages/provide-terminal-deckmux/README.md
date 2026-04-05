<!-- SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved. -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# provide-terminal-deckmux

Collaborative terminal presence — see who's connected, where they're looking, and who has keyboard control.

**DeckMux** = **Deck** (terminal screen / command deck) + **Mux** (multiplexer). The user who owns the session is the **DM** (the one running the session).

## Installation

```bash
pip install provide-terminal-deckmux
```

Standalone package with zero required dependencies. When installed alongside `provide-terminal`, the TermHub integration mixin activates automatically.

## Features

### Presence tracking

- **Avatar bar** — colored circles with 2-letter initials, role badges (admin/operator/viewer), idle and typing indicators
- **Edge indicators** — minimap-style viewport bars on the terminal's right edge showing where each user is scrolled
- **Pinned cursors** — click a line to pin your position; visible to all watchers as a colored label
- **Text selection sharing** — select text and others see a semi-transparent highlight in your color

### Control transfer

Three modes of control handover:

- **Request / handover** — click the owner's avatar, request control; owner sees a toast and can accept or deny
- **Admin takeover** — admins can take control immediately without a request
- **Auto-transfer** — if the owner goes idle while another user is actively typing, control transfers automatically after a configurable timeout

### Keystroke queue

Non-owners can type while waiting for control. Their keystrokes are buffered and displayed next to their avatar. On control transfer, the queue is either:

- `"replay"` — keystrokes are sent to the terminal (zero-loss handoff)
- `"display"` — keystrokes are cleared (display-only, no side effects)

### Identity

- **JWT users** — display name extracted from `name`, `preferred_username`, `email`, or `sub` claims
- **Anonymous / dev mode** — deterministic adjective+animal name from connection ID (1024 unique combinations)
- **Colors** — deterministic from user ID hash, 12 high-contrast colors, collision-avoiding within a session

## Per-Session Configuration

DeckMux is enabled per session. Add these fields to your session definition:

```toml
[sessions.debug]
presence = true
auto_transfer_idle_s = 30
keystroke_queue = "replay"
```

| Field | Type | Default | Description |
|---|---|---|---|
| `presence` | `bool` | `false` | Enable DeckMux for this session |
| `auto_transfer_idle_s` | `int` | `30` | Seconds before idle owner loses control (0 = disabled) |
| `keystroke_queue` | `"display" \| "replay"` | `"display"` | What happens to queued keystrokes on transfer |

Quick-connect (`POST /api/connect`) accepts these fields in the payload. The `hello` message includes `presence_enabled`, `auto_transfer_idle_s`, and `keystroke_queue` so the frontend knows how to initialize.

## Protocol

All DeckMux messages ride the existing WebSocket control channel (DLE+STX JSON framing). No new endpoints required.

### Message types

| Direction | Type | Purpose |
|---|---|---|
| Browser -> Server | `presence_update` | Scroll position, selection, pin, typing state |
| Browser -> Server | `queued_input` | Buffered keystrokes from non-owner |
| Browser -> Server | `control_request` | Request control from current owner |
| Browser -> Server | `control_handover` | Owner hands control to another user |
| Server -> Browser | `presence_update` | Relayed state for another user |
| Server -> Browser | `presence_sync` | Full state snapshot on join |
| Server -> Browser | `presence_leave` | User disconnected |
| Server -> Browser | `control_transfer` | Control changed hands |
| Server -> Browser | `auto_transfer_warning` | Owner about to lose control |
| Server -> Browser | `control_request_notification` | Someone is requesting control |
| Server -> Browser | `control_denied` | Request was denied |

All updates are event-driven with a 200ms debounce on the client side. Hibernation-compatible: presence state is ephemeral and reconstructed via re-announce on CF Worker wake.

## Architecture

```
packages/provide-terminal-deckmux/           <- standalone, zero deps
  src/provide/deckmux/
    _protocol.py                  <- message types & serialization
    _presence.py                  <- PresenceStore + UserPresence
    _names.py                     <- deterministic name & color generation
    _transfer.py                  <- TransferManager + keystroke queue
    _edge.py                      <- viewport range math

packages/provide-terminal/          <- integration layer
  src/provide/terminal/deckmux/
    _hub_mixin.py                 <- TermHub mixin (routes presence msgs)

packages/provide-terminal-frontend/ <- browser UI
  src/app/deckmux/
    presence-bar.ts               <- avatar bar widget
    edge-indicators.ts            <- minimap viewport bars
    cursor-overlay.ts             <- pins + selection highlights
    control-panel.ts              <- toasts, context menus, transfer UI
    keystroke-queue.ts            <- queued keystroke display
```

The mixin hooks into TermHub's existing broadcast, role checking, and lease management. It does not duplicate any of that logic.

## Quick Example

```python
from provide.terminal.server.models import SessionDefinition

session = SessionDefinition(
    id="pair-debug",
    connector_type="shell",
    presence=True,
    auto_transfer_idle_s=30,
    keystroke_queue="replay",
)
```

When a browser connects and receives `hello` with `presence_enabled: true`, the frontend initializes the avatar bar, edge indicators, and control panel automatically.

## Diagrams

See [`docs/diagrams/`](docs/diagrams/) for PlantUML sequence and architecture diagrams:

- [presence-join.puml](docs/diagrams/presence-join.puml) — browser joins session flow
- [presence-events.puml](docs/diagrams/presence-events.puml) — scroll, pin, select, disconnect events
- [control-transfer.puml](docs/diagrams/control-transfer.puml) — manual request and handover
- [auto-transfer.puml](docs/diagrams/auto-transfer.puml) — idle owner auto-transfer with keystroke queue
- [hibernation-wake.puml](docs/diagrams/hibernation-wake.puml) — CF Worker hibernation recovery
- [architecture.puml](docs/diagrams/architecture.puml) — package architecture overview

## Design Spec

Full design specification: [`docs/superpowers/specs/2026-03-30-deckmux-design.md`](../../docs/superpowers/specs/2026-03-30-deckmux-design.md)

## License

AGPL-3.0-or-later. Copyright (c) 2025-2026 provide.io llc.
