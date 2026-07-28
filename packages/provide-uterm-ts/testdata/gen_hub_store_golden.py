#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript hub-store port.

Three things are recorded rather than remembered.

The input buffer, because its overflow rule is not the obvious one: a buffer
that would exceed the cap is *discarded entirely* rather than truncated or
flushed, so an over-long paste vanishes instead of arriving in pieces. The
table drives the cap from both sides.

The hijack predicates, because ``StateStore.is_dashboard_hijack_active``
disagrees with ``HijackLease.is_dashboard_active`` on the same state: an owner
with **no** expiry reads as *active* here and *inactive* there. Both are in
the reference, and a port that quietly unified them would change who is
allowed to send input. The table records both answers side by side.

``clamp_lease``, because its bounds are a policy decision (matched to the WS
idle-reader timeout) rather than an arithmetic one.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_hub_store_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest import mock

from provide.uterm.bridge.coordinator import HijackSession
from provide.uterm.server.bridge.hub.store import StateStore
from provide.uterm.server.bridge.models import WorkerTermState

OUT = Path(__file__).with_name("hub_store_golden.json")

NOW = 1000.0
MAX_BUFFER_CHARS = 16

# (name, prior buffer contents, incoming data)
BUFFER_CASES: list[tuple[str, str, str]] = [
    ("no newline, empty buffer", "", "ls"),
    ("no newline, accumulating", "ls ", "-la"),
    ("linefeed completes a command", "ls", "\n"),
    ("carriage return completes a command", "ls", "\r"),
    ("crlf completes a command", "ls", "\r\n"),
    ("newline mid-payload keeps the tail", "", "ls\nrm -rf /"),
    ("newline alone", "", "\n"),
    ("exactly at the cap", "a" * 15, "b"),
    ("one past the cap", "a" * 16, "b"),
    ("over the cap in one write", "", "a" * 17),
    ("over the cap even with a newline", "", "a" * 20 + "\n"),
]

# (name, lease_s)
CLAMP_CASES: list[tuple[str, int]] = [
    ("below the floor", 0),
    ("negative", -5),
    ("at the floor", 1),
    ("typical", 90),
    ("at the ceiling", 14400),
    ("above the ceiling", 14401),
    ("absurd", 10**9),
]

# (name, has_owner, owner_expires_at, session_expires_at)
HIJACK_CASES: list[tuple[str, bool, float | None, float | None]] = [
    ("idle", False, None, None),
    ("dashboard owner, unexpired", True, NOW + 10.0, None),
    ("dashboard owner, expiring exactly now", True, NOW, None),
    ("dashboard owner, expired", True, NOW - 10.0, None),
    ("dashboard owner, no expiry", True, None, None),
    ("rest lease, unexpired", False, None, NOW + 10.0),
    ("rest lease, expiring exactly now", False, None, NOW),
    ("rest lease, expired", False, None, NOW - 10.0),
    ("dashboard expired, rest live", True, NOW - 10.0, NOW + 10.0),
]


class _FakeHub:
    """The subset of ``TermHub`` the store reaches through its back reference."""

    def __init__(self, max_buffer_chars: int = MAX_BUFFER_CHARS) -> None:
        self._input_buffers: dict[Any, str] = {}
        self.max_buffer_chars = max_buffer_chars
        self._on_metric: Any = None
        self._on_hijack_changed: Any = None


def _buffer_record() -> list[dict[str, Any]]:
    """What each write returns, and what is left buffered afterwards."""
    records = []
    for name, prior, data in BUFFER_CASES:
        hub = _FakeHub()
        store = StateStore(hub)  # type: ignore[arg-type]
        if prior:
            hub._input_buffers["ws"] = prior
        command = store.buffer_and_get_command("ws", data)  # type: ignore[arg-type]
        records.append(
            {
                "name": name,
                "prior": prior,
                "data": data,
                "command": command,
                "buffered_after": hub._input_buffers.get("ws"),
            }
        )
    return records


def _buffer_isolation_record() -> dict[str, Any]:
    """Two browsers must not share a buffer."""
    hub = _FakeHub()
    store = StateStore(hub)  # type: ignore[arg-type]
    store.buffer_and_get_command("ws1", "one")  # type: ignore[arg-type]
    store.buffer_and_get_command("ws2", "two")  # type: ignore[arg-type]
    return {
        "first": store.buffer_and_get_command("ws1", "\n"),  # type: ignore[arg-type]
        "second": store.buffer_and_get_command("ws2", "\n"),  # type: ignore[arg-type]
    }


def _state(has_owner: bool, owner_expires_at: float | None, session_expires_at: float | None) -> WorkerTermState:
    """Build a worker state with the hijack slots the cases vary."""
    state = WorkerTermState()
    if has_owner:
        state.hijack_owner = "ws"  # type: ignore[assignment]
        state.hijack_owner_expires_at = owner_expires_at
    if session_expires_at is not None:
        state.hijack_session = HijackSession(hijack_id="h1", owner="operator", lease_expires_at=session_expires_at)
    return state


def _hijack_record() -> list[dict[str, Any]]:
    """The store's predicates, next to the lease view's answer for the same state."""
    hub = _FakeHub()
    store = StateStore(hub)  # type: ignore[arg-type]
    records = []
    with mock.patch("time.monotonic", return_value=NOW):
        for name, has_owner, owner_expires_at, session_expires_at in HIJACK_CASES:
            state = _state(has_owner, owner_expires_at, session_expires_at)
            records.append(
                {
                    "name": name,
                    "has_owner": has_owner,
                    "owner_expires_at": owner_expires_at,
                    "session_expires_at": session_expires_at,
                    "has_valid_rest_lease": store.has_valid_rest_lease(state),
                    "is_dashboard_hijack_active": store.is_dashboard_hijack_active(state),
                    "is_hijacked": store.is_hijacked(state),
                    # The same question asked of the lease view, which answers
                    # the no-expiry case differently.
                    "lease_view_is_dashboard_active": state.lease.is_dashboard_active(NOW),
                }
            )
    return records


def _metric_record() -> dict[str, Any]:
    """The callback fan-out, including the int() coercion of the value."""
    hub = _FakeHub()
    store = StateStore(hub)  # type: ignore[arg-type]
    seen: list[tuple[str, int]] = []

    # No callback configured: the call is a no-op rather than an error.
    store.metric("never_seen")

    hub._on_metric = lambda name, value: seen.append((name, value))
    store.metric("default_value")
    store.metric("explicit", 5)
    store.metric("truncated", 2.9)  # type: ignore[arg-type]
    store.metric("negative", -3)

    def _raiser(name: str, value: int) -> None:
        raise RuntimeError("callback exploded")

    hub._on_metric = _raiser
    store.metric("raises")  # swallowed

    return {"seen": [list(entry) for entry in seen]}


def _raise_sync(worker_id: str, enabled: bool, owner: str | None) -> None:
    """A subscriber that raises synchronously."""
    raise RuntimeError("subscriber exploded")


def _notify_record() -> dict[str, Any]:
    """The hijack-changed fan-out, sync and awaitable."""
    hub = _FakeHub()
    store = StateStore(hub)  # type: ignore[arg-type]
    seen: list[tuple[str, bool, str | None]] = []

    store.notify_hijack_changed("w0", enabled=True)  # no callback: no-op

    hub._on_hijack_changed = lambda worker_id, enabled, owner: seen.append((worker_id, enabled, owner))
    store.notify_hijack_changed("w1", enabled=True, owner="operator")
    store.notify_hijack_changed("w2", enabled=False)

    async def _drive_async() -> None:
        async def _async_cb(worker_id: str, enabled: bool, owner: str | None) -> None:
            seen.append((worker_id, enabled, owner))

        hub._on_hijack_changed = _async_cb
        store.notify_hijack_changed("w3", enabled=True, owner="admin")
        # The awaitable is fired and forgotten: notify returns before it has
        # run, and it only lands once the loop gets a chance to schedule it.
        ran_before_yield = len(seen) == 3
        await asyncio.sleep(0)
        return ran_before_yield  # type: ignore[return-value]

    ran_before_yield = asyncio.run(_drive_async())

    # A synchronously-raising callback is NOT guarded: unlike metric(), the
    # call is unprotected and the exception reaches the caller.
    hub._on_hijack_changed = _raise_sync
    try:
        store.notify_hijack_changed("w6", enabled=True)
        sync_raise_propagates = False
    except RuntimeError:
        sync_raise_propagates = True

    return {
        "sync_raise_propagates": sync_raise_propagates,
        "seen": [list(entry) for entry in seen],
        "async_callback_ran_before_yield": ran_before_yield,
    }


def main() -> int:
    """Write the golden corpus and report the case count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_hub_store_golden.py",
        "now": NOW,
        "max_buffer_chars": MAX_BUFFER_CHARS,
        "buffers": _buffer_record(),
        "buffer_isolation": _buffer_isolation_record(),
        "clamps": [
            {"name": name, "lease_s": lease_s, "clamped": StateStore.clamp_lease(lease_s)}
            for name, lease_s in CLAMP_CASES
        ],
        "hijack": _hijack_record(),
        "metric": _metric_record(),
        "notify": _notify_record(),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(BUFFER_CASES)} buffer cases, {len(HIJACK_CASES)} hijack cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
