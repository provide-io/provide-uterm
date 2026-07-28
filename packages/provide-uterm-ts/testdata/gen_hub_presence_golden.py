#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript hub-presence port.

``can_send_input`` is the gate every browser input frame passes through, and
it is the one recorded here in full. Its two modes ask different questions: in
hijack mode only the lease holder may type, while in open mode the lease is
irrelevant and the *role* decides — viewers still cannot send, and a browser
the hub has never seen is treated as a viewer rather than being refused
outright.

The browser-state snapshot is the other half. It is what a resuming browser
is told about the session it is rejoining, and the shape for a worker the hub
does not know is a deliberate all-false default rather than an error, so a
browser attaching before its worker connects gets a usable answer.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_hub_presence_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest import mock

from provide.uterm.server.bridge.hub.presence import PresenceManager
from provide.uterm.server.bridge.hub.registry import WorkerRegistry
from provide.uterm.server.bridge.hub.store import StateStore
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

OUT = Path(__file__).with_name("hub_presence_golden.json")

NOW = 1000.0

# (name, input_mode, role for the asking browser, is the asker the owner)
INPUT_CASES: list[tuple[str, str, str | None, bool]] = [
    ("hijack mode, holder", "hijack", None, True),
    ("hijack mode, not the holder", "hijack", None, False),
    ("hijack mode, holder with an operator role", "hijack", "operator", True),
    ("hijack mode, admin who is not the holder", "hijack", "admin", False),
    ("open mode, viewer", "open", "viewer", False),
    ("open mode, operator", "open", "operator", False),
    ("open mode, admin", "open", "admin", False),
    ("open mode, unknown browser", "open", None, False),
    ("open mode, holder with no role", "open", None, True),
]

# (name, worker known, worker connected, input_mode, owner is the asker, owner expiry, rest lease)
SNAPSHOT_CASES: list[tuple[str, bool, bool, str, bool, float | None, float | None]] = [
    ("unknown worker", False, False, "hijack", False, None, None),
    ("idle worker", True, True, "hijack", False, None, None),
    ("worker offline", True, False, "hijack", False, None, None),
    ("hijacked by me", True, True, "hijack", True, NOW + 10.0, None),
    ("hijacked by someone else", True, True, "hijack", False, NOW + 10.0, None),
    ("my hijack has expired", True, True, "hijack", True, NOW - 10.0, None),
    ("rest lease held", True, True, "hijack", False, None, NOW + 10.0),
    ("open input mode", True, True, "open", False, None, None),
    ("perpetual hold by me", True, True, "hijack", True, None, None),
    # My hold lapsed and a REST client took the worker. The session is
    # hijacked, but it is emphatically not mine any more.
    ("my hold lapsed, rest took over", True, True, "hijack", True, NOW - 10.0, NOW + 10.0),
]


class _FakeHub:
    """The subset of ``TermHub`` the presence manager reaches back through."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.registry = WorkerRegistry()
        self.sent: list[dict[str, Any]] = []
        self.role = "viewer"

    def is_hijacked(self, st: WorkerTermState) -> bool:
        return StateStore.is_dashboard_hijack_active(st) or StateStore.has_valid_rest_lease(st)

    def is_dashboard_hijack_active(self, st: WorkerTermState) -> bool:
        return StateStore.is_dashboard_hijack_active(st)

    async def _resolve_role_for_browser(self, ws: Any, worker_id: str) -> str:
        return self.role

    async def send_worker(self, worker_id: str, msg: dict[str, Any], *, source: Any = None) -> bool:
        self.sent.append(msg)
        return True


def _input_record() -> list[dict[str, Any]]:
    """The per-frame input gate, in both modes."""
    hub = _FakeHub()
    presence = PresenceManager(hub)  # type: ignore[arg-type]
    records = []
    with mock.patch("time.monotonic", return_value=NOW):
        for name, input_mode, role, is_owner in INPUT_CASES:
            st = WorkerTermState()
            st.input_mode = input_mode  # type: ignore[assignment]
            st.hijack_owner = "asker" if is_owner else "someone-else"
            st.hijack_owner_expires_at = NOW + 10.0
            if role is not None:
                st.browsers = {"asker": role}  # type: ignore[dict-item]
            records.append(
                {
                    "name": name,
                    "input_mode": input_mode,
                    "role": role,
                    "is_owner": is_owner,
                    "allowed": presence.can_send_input(st, "asker"),  # type: ignore[arg-type]
                }
            )
    return records


async def _snapshot_record() -> list[dict[str, Any]]:
    """What a resuming browser is told about the session it is rejoining."""
    records = []
    for name, known, connected, input_mode, owner_is_asker, owner_expires, rest_expires in SNAPSHOT_CASES:
        hub = _FakeHub()
        presence = PresenceManager(hub)  # type: ignore[arg-type]
        if known:
            st = WorkerTermState()
            st.worker_ws = "worker" if connected else None  # type: ignore[assignment]
            st.input_mode = input_mode  # type: ignore[assignment]
            if owner_expires is not None or owner_is_asker:
                st.hijack_owner = "asker" if owner_is_asker else "someone-else"
                st.hijack_owner_expires_at = owner_expires
            if rest_expires is not None:
                st.hijack_session = HijackSession(hijack_id="h1", owner="cli", lease_expires_at=rest_expires)
            hub.registry._workers["w1"] = st
        with mock.patch("time.monotonic", return_value=NOW):
            snapshot = await presence.register_browser_state_snapshot("w1", "asker")  # type: ignore[arg-type]
        records.append({"name": name, **snapshot})
    return records


async def _control_frame_record() -> dict[str, Any]:
    """The worker-bound presence pokes, minus their random request ids."""
    hub = _FakeHub()
    presence = PresenceManager(hub)  # type: ignore[arg-type]
    with mock.patch("time.time", return_value=NOW):
        await presence.request_snapshot("w1")
        await presence.request_analysis("w1")
    return {
        "types": [msg["type"] for msg in hub.sent],
        "timestamps": [msg["ts"] for msg in hub.sent],
        "req_id_lengths": [len(msg["req_id"]) for msg in hub.sent],
        "req_ids_differ": hub.sent[0]["req_id"] != hub.sent[1]["req_id"],
        "keys": [sorted(msg) for msg in hub.sent],
    }


async def _role_record() -> dict[str, Any]:
    """Role resolution is a straight pass-through to the hub's resolver."""
    hub = _FakeHub()
    presence = PresenceManager(hub)  # type: ignore[arg-type]
    resolved = []
    for role in ("viewer", "operator", "admin"):
        hub.role = role
        resolved.append(await presence.resolve_role_for_browser("asker", "w1"))  # type: ignore[arg-type]
    return {"resolved": resolved}


async def _build_payload() -> dict[str, Any]:
    """Assemble every recorded section."""
    return {
        "generator": "packages/provide-uterm-ts/testdata/gen_hub_presence_golden.py",
        "now": NOW,
        "input": _input_record(),
        "snapshots": await _snapshot_record(),
        "control_frames": await _control_frame_record(),
        "roles": await _role_record(),
    }


def main() -> int:
    """Write the golden corpus and report the case count."""
    payload = asyncio.run(_build_payload())
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(INPUT_CASES)} input cases, {len(SNAPSHOT_CASES)} snapshot cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
