#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript hub-connection port.

Every value here is *driven through the real* :class:`ConnectionManager`
against a fake hub, not re-derived — a corpus that reimplements the reference
would only prove the port matches my reading of it.

Three of the decisions recorded carry a scar each.

**A worker reconnect must not invalidate a live lease.** Worker sockets drop
routinely — a Durable Object rotating, a manager restarting, a network blip —
and clearing the hijack on every register meant one blip silently invalidated
the holder's hijack id, every later send 404'd, and a long run cratered.
Time-bounded expiry is the security guarantee; a WebSocket reconnect is not a
security event, so the lease is cleared only when it has actually expired.

**The worker cap counts new ids only.** A reconnecting worker that is already
registered is admitted even at capacity, or the cap turns a full hub into one
that can never heal.

**The per-principal browser quota must balance exactly.** The increment
happens before the rest of registration, so anything raising afterwards rolls
it back — otherwise a failed connect leaks a slot, nothing reaps the counter,
and the principal is locked out at their limit forever.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_hub_connection_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest import mock

from fastapi import WebSocketException

from provide.uterm.server.bridge.hub.connection import ConnectionManager
from provide.uterm.server.bridge.hub.registry import WorkerRegistry
from provide.uterm.server.bridge.hub.store import StateStore
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

OUT = Path(__file__).with_name("hub_connection_golden.json")

NOW = 1000.0
EVENT_DEQUE_MAXLEN = 2000

# (name, has existing state, rest lease expiry, dashboard owner present)
REGISTER_CASES: list[tuple[str, bool, float | None, bool]] = [
    ("first connection", False, None, False),
    ("reconnect, nothing held", True, None, False),
    ("reconnect, live REST lease", True, NOW + 60.0, False),
    ("reconnect, REST lease expiring exactly now", True, NOW, False),
    ("reconnect, expired REST lease", True, NOW - 60.0, False),
    ("reconnect, dashboard owner only", True, None, True),
    ("reconnect, live REST lease and a dashboard owner", True, NOW + 60.0, True),
    ("reconnect, expired REST lease and a dashboard owner", True, NOW - 60.0, True),
]

# (name, existing worker ids, cap, id being registered)
CAP_CASES: list[tuple[str, list[str], int, str]] = [
    ("first worker, room to spare", [], 2, "w1"),
    ("second worker, at the edge", ["w1"], 2, "w2"),
    ("new worker at capacity", ["w1", "w2"], 2, "w3"),
    ("reconnect at capacity", ["w1", "w2"], 2, "w1"),
    ("reconnect over capacity", ["w1", "w2", "w3"], 2, "w2"),
    ("new worker with a zero cap", [], 0, "w1"),
]

# (name, existing count for the principal, cap)
QUOTA_CASES: list[tuple[str, int, int]] = [
    ("first connection", 0, 2),
    ("second connection", 1, 2),
    ("third connection at a cap of two", 2, 2),
    ("well over the cap", 5, 2),
    ("zero cap", 0, 0),
]

# (name, event types in the log, oldest first)
RESUME_SCAN_CASES: list[tuple[str, list[Any]]] = [
    ("empty history", []),
    ("only output", ["term", "term"]),
    ("owner expired", ["term", "hijack_owner_expired"]),
    ("lease expired", ["term", "hijack_lease_expired"]),
    ("expiry then a snapshot", ["hijack_owner_expired", "snapshot"]),
    ("acquired then output", ["hijack_acquired", "term"]),
    ("released then output", ["hijack_released", "term"]),
    ("acquired after an expiry", ["hijack_owner_expired", "hijack_acquired"]),
    ("expiry after an acquire", ["hijack_acquired", "hijack_owner_expired"]),
    ("malformed entry before an expiry", [7, "hijack_owner_expired"]),
    ("malformed entry after an expiry", ["hijack_owner_expired", 7]),
]


class _Principal:
    """A minimal authenticated principal."""

    def __init__(self, subject_id: Any) -> None:
        self.subject_id = subject_id


class _WSState:
    """Stand-in for a Starlette WebSocket's ``state`` bag."""

    def __init__(self, principal: Any) -> None:
        self.uterm_principal = principal


class _WS:
    """A browser or worker socket identified only by name."""

    def __init__(self, name: str, principal: Any = None) -> None:
        self.name = name
        if principal is not None:
            self.state = _WSState(principal)

    def __repr__(self) -> str:
        return self.name


class _FakeHub:
    """The subset of ``TermHub`` the connection manager reaches through."""

    def __init__(self, *, max_workers: int = 100, max_connections_per_principal: int = 100) -> None:
        self._lock = asyncio.Lock()
        self.registry = WorkerRegistry()
        self.max_workers = max_workers
        self.max_connections_per_principal = max_connections_per_principal
        self._event_deque_maxlen = EVENT_DEQUE_MAXLEN
        self._worker_token = None
        self._resume_store = None
        self._resume_ttl_s = 60
        self._ws_to_resume_token: dict[Any, str] = {}
        self._ws_principal: dict[Any, str] = {}
        self._principal_browser_counts: dict[str, int] = {}
        self._startup_pending_browsers: set[Any] = set()
        self._background_tasks: set[Any] = set()
        self._output_policy_gate = None

    def is_hijacked(self, st: WorkerTermState) -> bool:
        return StateStore.is_dashboard_hijack_active(st) or StateStore.has_valid_rest_lease(st)

    def is_dashboard_hijack_active(self, st: WorkerTermState) -> bool:
        return StateStore.is_dashboard_hijack_active(st)

    def has_valid_rest_lease(self, st: WorkerTermState) -> bool:
        return StateStore.has_valid_rest_lease(st)


def _state(rest_expiry: float | None, has_owner: bool) -> WorkerTermState:
    """A worker state holding the leases a case needs."""
    st = WorkerTermState()
    if rest_expiry is not None:
        st.hijack_session = HijackSession(hijack_id="h1", owner="cli", lease_expires_at=rest_expiry)
    if has_owner:
        st.hijack_owner = _WS("old-browser")  # type: ignore[assignment]
        st.hijack_owner_expires_at = NOW + 60.0
    return st


async def _register_record() -> list[dict[str, Any]]:
    """What register_worker keeps and what it clears."""
    records = []
    for name, has_state, rest_expiry, has_owner in REGISTER_CASES:
        hub = _FakeHub()
        manager = ConnectionManager(hub)  # type: ignore[arg-type]
        if has_state:
            hub.registry._workers["w1"] = _state(rest_expiry, has_owner)
        ws = _WS("worker")
        with mock.patch("time.monotonic", return_value=NOW):
            prev_was_hijacked = await manager.register_worker("w1", ws)  # type: ignore[arg-type]
        st = hub.registry._workers["w1"]
        records.append(
            {
                "name": name,
                "has_state": has_state,
                "rest_expiry": rest_expiry,
                "has_owner": has_owner,
                "prev_was_hijacked": prev_was_hijacked,
                "session_cleared": st.hijack_session is None,
                "session_after": None if st.hijack_session is None else st.hijack_session.lease_expires_at,
                "owner_cleared": st.hijack_owner is None,
                "worker_attached": st.worker_ws is ws,
            }
        )
    return records


async def _cap_record() -> list[dict[str, Any]]:
    """Whether the worker cap admits a registration."""
    records = []
    for name, existing, cap, worker_id in CAP_CASES:
        hub = _FakeHub(max_workers=cap)
        manager = ConnectionManager(hub)  # type: ignore[arg-type]
        for existing_id in existing:
            hub.registry._workers[existing_id] = WorkerTermState()
        with mock.patch("time.monotonic", return_value=NOW):
            try:
                await manager.register_worker(worker_id, _WS("worker"))  # type: ignore[arg-type]
                admitted, reason, code = True, None, None
            except WebSocketException as exc:
                admitted, reason, code = False, exc.reason, exc.code
        records.append(
            {
                "name": name,
                "existing": existing,
                "cap": cap,
                "worker_id": worker_id,
                "admitted": admitted,
                "reason": reason,
                "code": code,
                "workers_after": sorted(hub.registry._workers),
            }
        )
    return records


async def _quota_record() -> list[dict[str, Any]]:
    """Whether the per-principal browser quota admits a connection."""
    records = []
    for name, current, cap in QUOTA_CASES:
        hub = _FakeHub(max_connections_per_principal=cap)
        manager = ConnectionManager(hub)  # type: ignore[arg-type]
        hub._principal_browser_counts["alice"] = current
        ws = _WS("browser", _Principal("alice"))
        with mock.patch("time.monotonic", return_value=NOW):
            try:
                await manager.register_browser("w1", ws, "operator")  # type: ignore[arg-type]
                admitted, reason, code = True, None, None
            except WebSocketException as exc:
                admitted, reason, code = False, exc.reason, exc.code
        records.append(
            {
                "name": name,
                "current": current,
                "cap": cap,
                "admitted": admitted,
                "reason": reason,
                "code": code,
                "after": hub._principal_browser_counts.get("alice", 0),
            }
        )
    return records


async def _exempt_record() -> dict[str, Any]:
    """Which principals the quota counts at all."""
    outcomes: dict[str, Any] = {}
    for label, principal in (
        ("no principal", None),
        ("anonymous", _Principal("anonymous")),
        ("empty subject", _Principal("")),
        ("non-string subject", _Principal(7)),
        ("named subject", _Principal("alice")),
    ):
        hub = _FakeHub(max_connections_per_principal=1)
        manager = ConnectionManager(hub)  # type: ignore[arg-type]
        ws = _WS("browser", principal)
        with mock.patch("time.monotonic", return_value=NOW):
            await manager.register_browser("w1", ws, "viewer")  # type: ignore[arg-type]
        outcomes[label] = {
            "counted": bool(hub._principal_browser_counts),
            "tracked": dict(hub._principal_browser_counts),
        }
    return outcomes


async def _rollback_record() -> dict[str, Any]:
    """A failure after the increment must leave the count untouched."""

    class _ExplodingResumeStore:
        async def create(self, worker_id: str, role: str, ttl_s: int) -> str:
            raise RuntimeError("resume store unavailable")

    hub = _FakeHub(max_connections_per_principal=2)
    manager = ConnectionManager(hub)  # type: ignore[arg-type]
    hub._resume_store = _ExplodingResumeStore()  # type: ignore[assignment]
    ws = _WS("browser", _Principal("alice"))
    with mock.patch("time.monotonic", return_value=NOW):
        try:
            await manager.register_browser("w1", ws, "operator")  # type: ignore[arg-type]
            raised = False
        except RuntimeError:
            raised = True
    return {
        "raised": raised,
        "count_after": hub._principal_browser_counts.get("alice", 0),
        "principal_tracked": ws in hub._ws_principal,
        "resume_token_tracked": ws in hub._ws_to_resume_token,
    }


async def _disconnect_record() -> list[dict[str, Any]]:
    """Disconnect outcomes, and the per-principal decrement."""
    records = []
    for name, is_owner, rest_expiry, owned_hijack in (
        ("plain viewer leaves", False, None, False),
        ("holder leaves", True, None, False),
        ("holder leaves with a live REST lease", True, NOW + 60.0, False),
        ("non-holder leaves claiming ownership", False, None, True),
        ("non-holder leaves with a live REST lease", False, NOW + 60.0, True),
    ):
        hub = _FakeHub(max_connections_per_principal=5)
        manager = ConnectionManager(hub)  # type: ignore[arg-type]
        ws = _WS("browser", _Principal("alice"))
        with mock.patch("time.monotonic", return_value=NOW):
            await manager.register_browser("w1", ws, "operator")  # type: ignore[arg-type]
            st = hub.registry._workers["w1"]
            st.worker_ws = _WS("worker")  # type: ignore[assignment]
            if rest_expiry is not None:
                st.hijack_session = HijackSession(hijack_id="h1", owner="cli", lease_expires_at=rest_expiry)
            if is_owner:
                st.hijack_owner = ws  # type: ignore[assignment]
                st.hijack_owner_expires_at = NOW + 60.0
            outcome = await manager.cleanup_browser_disconnect("w1", ws, owned_hijack)  # type: ignore[arg-type]
        records.append(
            {
                "name": name,
                "is_owner": is_owner,
                "rest_expiry": rest_expiry,
                "owned_hijack": owned_hijack,
                **outcome,
                "count_after": hub._principal_browser_counts.get("alice", 0),
                "browsers_left": len(hub.registry._workers["w1"].browsers),
            }
        )
    return records


async def _resume_scan_record() -> list[dict[str, Any]]:
    """The backwards scan that decides whether a resume is still needed."""
    records = []
    for name, events in RESUME_SCAN_CASES:
        st = WorkerTermState()
        for event_type in events:
            st.events.append({"type": event_type})
        records.append(
            {
                "name": name,
                "events": events,
                "resume_needed": ConnectionManager._scan_events_for_resume(st),
            }
        )
    return records


async def _hello_record() -> list[dict[str, Any]]:
    """worker_hello: input mode and the protocol version it records."""
    records = []
    for name, exists, mode, rest_expiry, has_owner, version in (
        ("unknown worker", False, "open", None, False, None),
        ("switch to open when idle", True, "open", None, False, None),
        ("switch to open while a REST lease is held", True, "open", NOW + 60.0, False, None),
        ("switch to open while a dashboard hold is active", True, "open", None, True, None),
        ("switch to hijack while held", True, "hijack", NOW + 60.0, False, None),
        ("with a protocol version", True, "hijack", None, False, 2),
        ("with a legacy protocol version", True, "hijack", None, False, 0),
    ):
        hub = _FakeHub()
        manager = ConnectionManager(hub)  # type: ignore[arg-type]
        if exists:
            hub.registry._workers["w1"] = _state(rest_expiry, has_owner)
        with mock.patch("time.monotonic", return_value=NOW):
            applied = await manager.set_worker_hello("w1", mode, version)  # type: ignore[arg-type]
        st = hub.registry._workers.get("w1")
        records.append(
            {
                "name": name,
                "exists": exists,
                "mode": mode,
                "rest_expiry": rest_expiry,
                "has_owner": has_owner,
                "version": version,
                "applied": applied,
                "input_mode_after": None if st is None else st.input_mode,
                "protocol_version_after": None if st is None else st.protocol_version,
            }
        )
    return records


async def _deregister_record() -> list[dict[str, Any]]:
    """Deregistration only fires for the socket that is still current."""
    records = []
    for name, same_socket, rest_expiry, has_owner in (
        ("current socket, nothing held", True, None, False),
        ("current socket, REST lease held", True, NOW + 60.0, False),
        ("current socket, dashboard hold", True, None, True),
        ("superseded socket", False, NOW + 60.0, False),
    ):
        hub = _FakeHub()
        manager = ConnectionManager(hub)  # type: ignore[arg-type]
        ws = _WS("worker")
        st = _state(rest_expiry, has_owner)
        st.worker_ws = ws if same_socket else _WS("replacement")  # type: ignore[assignment]
        hub.registry._workers["w1"] = st
        with mock.patch("time.monotonic", return_value=NOW):
            broadcast, was_hijacked = await manager.deregister_worker("w1", ws)  # type: ignore[arg-type]
        records.append(
            {
                "name": name,
                "same_socket": same_socket,
                "rest_expiry": rest_expiry,
                "has_owner": has_owner,
                "should_broadcast": broadcast,
                "was_hijacked": was_hijacked,
                "worker_cleared": st.worker_ws is None,
                "session_cleared": st.hijack_session is None,
                "owner_cleared": st.hijack_owner is None,
            }
        )
    return records


async def _build_payload() -> dict[str, Any]:
    """Assemble every recorded section."""
    return {
        "generator": "packages/provide-uterm-ts/testdata/gen_hub_connection_golden.py",
        "now": NOW,
        "registers": await _register_record(),
        "worker_caps": await _cap_record(),
        "principal_quotas": await _quota_record(),
        "quota_exemptions": await _exempt_record(),
        "quota_rollback": await _rollback_record(),
        "disconnects": await _disconnect_record(),
        "resume_scans": await _resume_scan_record(),
        "hellos": await _hello_record(),
        "deregisters": await _deregister_record(),
    }


def main() -> int:
    """Write the golden corpus and report the case count."""
    payload = asyncio.run(_build_payload())
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(payload['registers'])} register cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
