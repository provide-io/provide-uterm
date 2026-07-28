#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript hub-lease port.

``HijackLeaseManager`` decides who is allowed to drive a worker, so its
refusal reasons and its ordering are the security surface — this is the
module the Python side holds at ``killed==100`` mutation coverage. Everything
recorded here is a decision the port must reproduce exactly, not a shape.

Three areas carry most of the weight.

**Refusal reasons.** ``try_acquire_rest`` and ``try_acquire_ws`` return a
reason string, and callers surface it. A port that returned the right boolean
with the wrong reason would look fine in a smoke test and be wrong in the API.
The tables drive every guard, in the order the reference checks them.

**The two-phase REST reserve.** The worker-pause send runs *outside* the hub
lock — holding a single global lock across a socket write lets one
backpressured worker stall the whole hub — so the slot is reserved with
``hijack_pending`` first. The corpus records that a dashboard acquire during
that window is refused, that a failed pause clears ``worker_ws`` and rolls the
reservation back, and that a superseded reservation does not clobber whoever
took the slot.

**Ownership checks.** Release and heartbeat verify *identity*, not just
presence: a heartbeat quoting the right hijack id but the wrong owner is
denied, and releasing a lease you do not hold is refused while reporting
whether a REST lease is still live.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_hub_lease_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest import mock

from provide.uterm.server.bridge.hub.lease import HijackLeaseManager
from provide.uterm.server.bridge.hub.registry import WorkerRegistry
from provide.uterm.server.bridge.hub.store import StateStore
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

OUT = Path(__file__).with_name("hub_lease_golden.json")

NOW = 1000.0
DASHBOARD_LEASE_S = 30

# (name, requested ttl) for the dashboard-lease clamp.
CLAMP_CASES: list[tuple[str, int]] = [
    ("below the floor", 0),
    ("negative", -1),
    ("at the floor", 1),
    ("typical", 30),
    ("at the ceiling", 600),
    ("above the ceiling", 601),
]


class _FakeWorkerWS:
    """A worker socket that records what was sent, and can be made to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def send_text(self, payload: str) -> None:
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(payload)


class _FakeHub:
    """The callback surface the lease manager reaches back through."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.hijacked_override: bool | None = None

    # -- predicates (delegated to the real StateStore implementations) --
    def is_dashboard_hijack_active(self, st: WorkerTermState) -> bool:
        return StateStore.is_dashboard_hijack_active(st)

    def has_valid_rest_lease(self, st: WorkerTermState) -> bool:
        return StateStore.has_valid_rest_lease(st)

    def is_hijacked(self, st: WorkerTermState) -> bool:
        if self.hijacked_override is not None:
            return self.hijacked_override
        return self.is_dashboard_hijack_active(st) or self.has_valid_rest_lease(st)

    def can_send_input(self, st: WorkerTermState, ws: Any) -> bool:
        if st.input_mode == "open":
            return st.browsers.get(ws, "viewer") in ("operator", "admin")
        return self.is_dashboard_hijack_active(st) and st.hijack_owner is ws

    # -- side effects (recorded) --
    def metric(self, name: str, value: int = 1) -> None:
        self.calls.append({"call": "metric", "name": name, "value": value})

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        self.calls.append({"call": "notify_hijack_changed", "worker_id": worker_id, "enabled": enabled, "owner": owner})

    async def send_worker(self, worker_id: str, msg: dict[str, Any], *, source: Any = None) -> bool:
        self.sent.append(msg)
        self.calls.append({"call": "send_worker", "worker_id": worker_id, "action": msg.get("action")})
        return True

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        self.calls.append({"call": "broadcast_hijack_state", "worker_id": worker_id})

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append({"call": "append_event", "worker_id": worker_id, "event_type": event_type})
        return {}

    async def prune_if_idle(self, worker_id: str) -> None:
        self.calls.append({"call": "prune_if_idle", "worker_id": worker_id})

    async def _recheck_and_resume(self, worker_id: str, now: float) -> None:
        self.calls.append({"call": "recheck_and_resume", "worker_id": worker_id})


def _build(dashboard_lease_s: int = DASHBOARD_LEASE_S) -> tuple[HijackLeaseManager, WorkerRegistry, _FakeHub]:
    """A manager over a fresh registry and a recording hub."""
    registry = WorkerRegistry()
    hub = _FakeHub()
    manager = HijackLeaseManager(registry, asyncio.Lock(), dashboard_lease_s, hub)  # type: ignore[arg-type]
    return manager, registry, hub


def _worker(
    registry: WorkerRegistry,
    *,
    connected: bool = True,
    input_mode: str = "hijack",
    owner: Any = None,
    owner_expires_at: float | None = None,
    session: HijackSession | None = None,
    pending: str | None = None,
    fail_send: bool = False,
) -> WorkerTermState:
    """Register a worker in the state the case under test needs."""
    st = WorkerTermState()
    if connected:
        st.worker_ws = _FakeWorkerWS(fail=fail_send)  # type: ignore[assignment]
    st.input_mode = input_mode  # type: ignore[assignment]
    st.hijack_owner = owner
    st.hijack_owner_expires_at = owner_expires_at
    st.hijack_session = session
    st.hijack_pending = pending
    registry._workers["w1"] = st
    return st


def _session(expires_at: float, *, owner: str = "operator", hijack_id: str = "h1") -> HijackSession:
    """A REST lease expiring at *expires_at*."""
    return HijackSession(
        hijack_id=hijack_id, owner=owner, acquired_at=NOW, lease_expires_at=expires_at, last_heartbeat=NOW
    )


def _clamp_record() -> list[dict[str, Any]]:
    """The dashboard-lease TTL clamp, at construction and via the setter."""
    records = []
    for name, requested in CLAMP_CASES:
        manager, _, _ = _build(requested)
        constructed = manager.dashboard_hijack_lease_s
        manager.dashboard_hijack_lease_s = requested
        records.append(
            {
                "name": name,
                "requested": requested,
                "clamped": constructed,
                "via_setter": manager.dashboard_hijack_lease_s,
            }
        )
    return records


def _compute_expirations_record() -> list[dict[str, Any]]:
    """compute_lease_expirations, which reads without mutating."""
    cases = [
        ("idle", None, None, None),
        ("browser live", "ws", NOW + 10.0, None),
        ("browser exactly now", "ws", NOW, None),
        ("browser expired", "ws", NOW - 10.0, None),
        ("browser without expiry", "ws", None, None),
        ("rest live", None, None, NOW + 10.0),
        ("rest exactly now", None, None, NOW),
        ("rest expired", None, None, NOW - 10.0),
        ("both expired", "ws", NOW - 1.0, NOW - 1.0),
    ]
    records = []
    for name, owner, owner_expires, session_expires in cases:
        st = WorkerTermState()
        st.hijack_owner = owner
        st.hijack_owner_expires_at = owner_expires
        st.hijack_session = None if session_expires is None else _session(session_expires)
        browser_expired, rest_expired = HijackLeaseManager.compute_lease_expirations(st, NOW)
        records.append(
            {
                "name": name,
                "owner": owner,
                "owner_expires_at": owner_expires,
                "session_expires_at": session_expires,
                "browser_expired": browser_expired,
                "rest_expired": rest_expired,
                # The state must be unchanged: this is a read.
                "owner_after": st.hijack_owner,
                "session_after": None if st.hijack_session is None else st.hijack_session.lease_expires_at,
            }
        )
    return records


async def _acquire_rest_case(
    name: str,
    *,
    exists: bool = True,
    connected: bool = True,
    input_mode: str = "hijack",
    owner: Any = None,
    owner_expires_at: float | None = None,
    session: HijackSession | None = None,
    pending: str | None = None,
    fail_send: bool = False,
) -> dict[str, Any]:
    """Drive one try_acquire_rest and record the decision and the state after."""
    manager, registry, _ = _build()
    st = None
    if exists:
        st = _worker(
            registry,
            connected=connected,
            input_mode=input_mode,
            owner=owner,
            owner_expires_at=owner_expires_at,
            session=session,
            pending=pending,
            fail_send=fail_send,
        )
    with mock.patch("time.monotonic", return_value=NOW), mock.patch("time.time", return_value=NOW):
        ok, reason = await manager.try_acquire_rest("w1", owner="cli", lease_s=90, hijack_id="new", now=NOW)
    after = registry._workers.get("w1")
    return {
        "name": name,
        "ok": ok,
        "reason": reason,
        "session_hijack_id": None if after is None or after.hijack_session is None else after.hijack_session.hijack_id,
        "session_expires_at": None
        if after is None or after.hijack_session is None
        else after.hijack_session.lease_expires_at,
        "pending_after": None if after is None else after.hijack_pending,
        "worker_ws_cleared": after is not None and after.worker_ws is None,
        "pause_sent": st is not None and st.worker_ws is not None and len(st.worker_ws.sent) > 0,  # type: ignore[union-attr]
    }


async def _acquire_rest_record() -> list[dict[str, Any]]:
    """Every guard on the REST acquire path, in the order the reference checks."""
    return [
        await _acquire_rest_case("unknown worker", exists=False),
        await _acquire_rest_case("worker not connected", connected=False),
        await _acquire_rest_case("open input mode", input_mode="open"),
        await _acquire_rest_case("dashboard hijack held", owner="ws", owner_expires_at=NOW + 10.0),
        await _acquire_rest_case("rest lease held", session=_session(NOW + 10.0)),
        await _acquire_rest_case("another acquire reserving", pending="other"),
        await _acquire_rest_case("expired dashboard lease does not block", owner="ws", owner_expires_at=NOW - 10.0),
        await _acquire_rest_case("expired rest lease does not block", session=_session(NOW - 10.0)),
        await _acquire_rest_case("pause send fails", fail_send=True),
        await _acquire_rest_case("granted"),
    ]


async def _acquire_ws_record() -> list[dict[str, Any]]:
    """Every guard on the dashboard acquire path."""
    cases: list[tuple[str, dict[str, Any]]] = [
        ("unknown worker", {"exists": False}),
        ("worker not connected", {"connected": False}),
        ("dashboard hijack held", {"owner": "other", "owner_expires_at": NOW + 10.0}),
        ("rest lease held", {"session": _session(NOW + 10.0)}),
        ("another acquire reserving", {"pending": "other"}),
        ("open input mode is not a guard here", {"input_mode": "open"}),
        ("granted", {}),
    ]
    records = []
    for name, kwargs in cases:
        manager, registry, _ = _build()
        exists = kwargs.pop("exists", True)
        if exists:
            _worker(registry, **kwargs)
        with mock.patch("time.monotonic", return_value=NOW):
            ok, reason = await manager.try_acquire_ws("w1", "browser")  # type: ignore[arg-type]
        after = registry._workers.get("w1")
        records.append(
            {
                "name": name,
                "ok": ok,
                "reason": reason,
                "owner_is_browser": after is not None and after.hijack_owner == "browser",
                "owner_expires_at": None if after is None else after.hijack_owner_expires_at,
            }
        )
    return records


async def _touch_record() -> dict[str, Any]:
    """touch_owner extends any owner; touch_if_owner verifies identity first."""
    manager, registry, _ = _build()
    _worker(registry, owner="browser", owner_expires_at=NOW + 1.0)
    with mock.patch("time.monotonic", return_value=NOW):
        default_ttl = await manager.touch_owner("w1")
        explicit = await manager.touch_owner("w1", 120)
        clamped_high = await manager.touch_owner("w1", 9999)
        clamped_low = await manager.touch_owner("w1", 0)
        unknown = await manager.touch_owner("nope")

        matching = await manager.touch_if_owner("w1", "browser")  # type: ignore[arg-type]
        mismatched = await manager.touch_if_owner("w1", "other")  # type: ignore[arg-type]

    # An owner slot with no expiry: touch_owner extends it, and touch_if_owner
    # sees it as active (the store predicate treats it as a perpetual hold).
    manager2, registry2, _ = _build()
    _worker(registry2, owner="browser", owner_expires_at=None)
    with mock.patch("time.monotonic", return_value=NOW):
        perpetual_touch = await manager2.touch_if_owner("w1", "browser")  # type: ignore[arg-type]

    # No owner at all.
    manager3, registry3, _ = _build()
    _worker(registry3)
    with mock.patch("time.monotonic", return_value=NOW):
        no_owner = await manager3.touch_owner("w1")

    return {
        "default_ttl": default_ttl,
        "explicit": explicit,
        "clamped_high": clamped_high,
        "clamped_low": clamped_low,
        "unknown": unknown,
        "matching": matching,
        "mismatched": mismatched,
        "perpetual_touch": perpetual_touch,
        "no_owner": no_owner,
    }


async def _release_record() -> dict[str, Any]:
    """Release verifies identity and reports whether a REST lease survives."""

    async def _release(owner: Any, owner_expires_at: float | None, session: HijackSession | None, ws: str) -> Any:
        manager, registry, _ = _build()
        _worker(registry, owner=owner, owner_expires_at=owner_expires_at, session=session)
        with mock.patch("time.monotonic", return_value=NOW):
            ok, rest_active = await manager.try_release_ws("w1", ws)  # type: ignore[arg-type]
        after = registry._workers["w1"]
        return {"ok": ok, "rest_active": rest_active, "owner_cleared": after.hijack_owner is None}

    manager, _, _ = _build()
    with mock.patch("time.monotonic", return_value=NOW):
        unknown = await manager.try_release_ws("nope", "browser")  # type: ignore[arg-type]

    return {
        "unknown_worker": list(unknown),
        "owner_matches": await _release("browser", NOW + 10.0, None, "browser"),
        "owner_mismatch": await _release("other", NOW + 10.0, None, "browser"),
        "owner_mismatch_with_rest": await _release("other", NOW + 10.0, _session(NOW + 10.0), "browser"),
        "released_with_rest_live": await _release("browser", NOW + 10.0, _session(NOW + 10.0), "browser"),
        "lease_expired": await _release("browser", NOW - 10.0, None, "browser"),
    }


async def _rest_lifecycle_record() -> dict[str, Any]:
    """Heartbeat, expiry re-read, validity and REST release."""
    manager, registry, hub = _build()
    _worker(registry, session=_session(NOW + 10.0))
    with mock.patch("time.monotonic", return_value=NOW):
        extended = await manager.extend_lease("w1", "h1", "operator", 90, NOW)
        wrong_id = await manager.extend_lease("w1", "nope", "operator", 90, NOW)
        wrong_owner = await manager.extend_lease("w1", "h1", "impostor", 90, NOW)
        unknown_worker = await manager.extend_lease("nope", "h1", "operator", 90, NOW)

        fresh = await manager.get_fresh_expiry("w1", "h1", 0.0)
        fresh_wrong_id = await manager.get_fresh_expiry("w1", "nope", -1.0)
        fresh_unknown = await manager.get_fresh_expiry("nope", "h1", -2.0)

        valid = await manager.check_valid("w1", "h1")
        valid_wrong_id = await manager.check_valid("w1", "nope")
        valid_unknown = await manager.check_valid("nope", "h1")

        released = await manager.release_rest("w1", "h1")
        released_twice = await manager.release_rest("w1", "h1")

    # Releasing while a dashboard lease is held must not ask for a resume.
    manager2, registry2, _ = _build()
    _worker(registry2, owner="browser", owner_expires_at=NOW + 10.0, session=_session(NOW + 10.0))
    with mock.patch("time.monotonic", return_value=NOW):
        released_with_dashboard = await manager2.release_rest("w1", "h1")

    # An expired session is not valid even though it is present.
    manager3, registry3, _ = _build()
    _worker(registry3, session=_session(NOW - 10.0))
    with mock.patch("time.monotonic", return_value=NOW):
        valid_expired = await manager3.check_valid("w1", "h1")

    return {
        "extended": extended,
        "wrong_id": wrong_id,
        "wrong_owner": wrong_owner,
        "wrong_owner_metric": [c["name"] for c in hub.calls if c["call"] == "metric"],
        "unknown_worker": unknown_worker,
        "fresh": fresh,
        "fresh_wrong_id": fresh_wrong_id,
        "fresh_unknown": fresh_unknown,
        "valid": valid,
        "valid_wrong_id": valid_wrong_id,
        "valid_unknown": valid_unknown,
        "valid_expired": valid_expired,
        "released": list(released),
        "released_twice": list(released_twice),
        "released_with_dashboard": list(released_with_dashboard),
    }


async def _cleanup_record() -> dict[str, Any]:
    """The expiry sweep: what it returns, and the calls it makes in order."""

    async def _sweep(owner: Any, owner_expires: float | None, session: HijackSession | None) -> dict[str, Any]:
        manager, registry, hub = _build()
        _worker(registry, owner=owner, owner_expires_at=owner_expires, session=session)
        with mock.patch("time.monotonic", return_value=NOW), mock.patch("time.time", return_value=NOW):
            changed = await manager.cleanup_expired("w1")
        after = registry._workers["w1"]
        return {
            "changed": changed,
            "calls": [
                c["call"] if c["call"] != "append_event" else f"append_event:{c['event_type']}" for c in hub.calls
            ],
            "owner_cleared": after.hijack_owner is None,
            "session_cleared": after.hijack_session is None,
        }

    manager, _, _ = _build()
    with mock.patch("time.monotonic", return_value=NOW):
        unknown = await manager.cleanup_expired("nope")

    return {
        "unknown_worker": unknown,
        "idle": await _sweep(None, None, None),
        "nothing_expired": await _sweep("browser", NOW + 10.0, _session(NOW + 10.0)),
        "dashboard_expired": await _sweep("browser", NOW - 10.0, None),
        "rest_expired": await _sweep(None, None, _session(NOW - 10.0)),
        "both_expired": await _sweep("browser", NOW - 10.0, _session(NOW - 10.0)),
        "dashboard_expired_rest_live": await _sweep("browser", NOW - 10.0, _session(NOW + 10.0)),
    }


async def _dead_browser_record() -> dict[str, Any]:
    """Removing dead sockets, and the resume that follows losing the owner."""

    async def _remove(
        owner: Any, owner_expires: float | None, session: HijackSession | None, dead: set[str]
    ) -> dict[str, Any]:
        manager, registry, hub = _build()
        st = _worker(registry, owner=owner, owner_expires_at=owner_expires, session=session)
        st.browsers = {"browser": "operator", "other": "viewer"}  # type: ignore[dict-item]
        with mock.patch("time.monotonic", return_value=NOW), mock.patch("time.time", return_value=NOW):
            notified = await manager.remove_dead_browsers("w1", dead)  # type: ignore[arg-type]
        after = registry._workers["w1"]
        return {
            "notified": notified,
            "browsers_left": sorted(after.browsers),
            "owner_cleared": after.hijack_owner is None,
            "calls": [c["call"] for c in hub.calls],
        }

    manager, _, _ = _build()
    unknown = await manager.remove_dead_browsers("nope", {"browser"})  # type: ignore[arg-type]

    return {
        "unknown_worker": unknown,
        "non_owner_died": await _remove("browser", NOW + 10.0, None, {"other"}),
        "owner_died": await _remove("browser", NOW + 10.0, None, {"browser"}),
        "owner_died_rest_live": await _remove("browser", NOW + 10.0, _session(NOW + 10.0), {"browser"}),
        "owner_died_lease_expired": await _remove("browser", NOW - 10.0, None, {"browser"}),
    }


async def _input_record() -> dict[str, Any]:
    """Input gating, and the lease extension that riding on it performs."""

    async def _prepare(input_mode: str, role: str | None, owner: Any, ws: str) -> dict[str, Any]:
        manager, registry, _ = _build()
        st = _worker(registry, input_mode=input_mode, owner=owner, owner_expires_at=NOW + 1.0 if owner else None)
        if role is not None:
            st.browsers = {ws: role}  # type: ignore[dict-item]
        with mock.patch("time.monotonic", return_value=NOW):
            allowed = await manager.prepare_browser_input("w1", ws)  # type: ignore[arg-type]
        after = registry._workers["w1"]
        return {"allowed": allowed, "owner_expires_at": after.hijack_owner_expires_at}

    manager, registry, _ = _build()
    _worker(registry, input_mode="open")
    with mock.patch("time.monotonic", return_value=NOW):
        open_mode = await manager.is_input_open_mode("w1")
        unknown_mode = await manager.is_input_open_mode("nope")
        unknown_prepare = await manager.prepare_browser_input("nope", "browser")  # type: ignore[arg-type]

    manager2, registry2, _ = _build()
    _worker(registry2, owner="browser", owner_expires_at=NOW + 10.0)
    with mock.patch("time.monotonic", return_value=NOW):
        hijacked = await manager2.still_hijacked("w1")
        hijacked_unknown = await manager2.still_hijacked("nope")

    return {
        "open_mode": open_mode,
        "unknown_mode": unknown_mode,
        "unknown_prepare": unknown_prepare,
        "still_hijacked": hijacked,
        "still_hijacked_unknown": hijacked_unknown,
        "hijack_mode_owner": await _prepare("hijack", None, "browser", "browser"),
        "hijack_mode_not_owner": await _prepare("hijack", None, "other", "browser"),
        "open_mode_viewer": await _prepare("open", "viewer", None, "browser"),
        "open_mode_operator": await _prepare("open", "operator", None, "browser"),
        "open_mode_admin": await _prepare("open", "admin", None, "browser"),
        "open_mode_unknown_role": await _prepare("open", None, None, "browser"),
    }


async def _events_record() -> dict[str, Any]:
    """The events window: sequence filter first, then the limit."""
    manager, registry, _ = _build()
    st = _worker(registry, session=_session(NOW + 10.0))
    for seq in range(1, 8):
        st.events.append({"seq": seq, "type": "output"})
    st.event_seq = 7
    st.min_event_seq = 1
    with mock.patch("time.monotonic", return_value=NOW):
        window = await manager.get_events_data("w1", "h1", _session(NOW + 5.0), after_seq=2, limit=3)
        all_rows = await manager.get_events_data("w1", "h1", _session(NOW + 5.0), after_seq=0, limit=100)
        # A hijack id that does not match the live session falls back to the
        # expiry the caller passed in.
        stale = await manager.get_events_data("w1", "other", _session(NOW + 5.0), after_seq=0, limit=1)
    return {
        "window_seqs": [row["seq"] for row in window["rows"]],
        "window_latest_seq": window["latest_seq"],
        "window_min_event_seq": window["min_event_seq"],
        "window_fresh_expires": window["fresh_expires"],
        "all_seqs": [row["seq"] for row in all_rows["rows"]],
        "stale_fresh_expires": stale["fresh_expires"],
    }


async def _get_session_record() -> dict[str, Any]:
    """get_rest_session cleans up first, so an expired lease reads as absent."""
    manager, registry, _ = _build()
    _worker(registry, session=_session(NOW + 10.0))
    with mock.patch("time.monotonic", return_value=NOW), mock.patch("time.time", return_value=NOW):
        live = await manager.get_rest_session("w1", "h1")
        wrong_id = await manager.get_rest_session("w1", "other")

    manager2, registry2, _ = _build()
    _worker(registry2, session=_session(NOW - 10.0))
    with mock.patch("time.monotonic", return_value=NOW), mock.patch("time.time", return_value=NOW):
        expired = await manager2.get_rest_session("w1", "h1")
        cleared = registry2._workers["w1"].hijack_session is None

    manager3, _, _ = _build()
    with mock.patch("time.monotonic", return_value=NOW), mock.patch("time.time", return_value=NOW):
        unknown = await manager3.get_rest_session("nope", "h1")

    return {
        "live_hijack_id": None if live is None else live.hijack_id,
        "wrong_id_is_none": wrong_id is None,
        "expired_is_none": expired is None,
        "expired_session_cleared": cleared,
        "unknown_is_none": unknown is None,
    }


async def _build_payload() -> dict[str, Any]:
    """Assemble every recorded section."""
    return {
        "generator": "packages/provide-uterm-ts/testdata/gen_hub_lease_golden.py",
        "now": NOW,
        "dashboard_lease_s": DASHBOARD_LEASE_S,
        "clamps": _clamp_record(),
        "compute_expirations": _compute_expirations_record(),
        "acquire_rest": await _acquire_rest_record(),
        "acquire_ws": await _acquire_ws_record(),
        "touch": await _touch_record(),
        "release": await _release_record(),
        "rest_lifecycle": await _rest_lifecycle_record(),
        "cleanup": await _cleanup_record(),
        "dead_browsers": await _dead_browser_record(),
        "input": await _input_record(),
        "events": await _events_record(),
        "get_session": await _get_session_record(),
    }


def main() -> int:
    """Write the golden corpus and report the section count."""
    payload = asyncio.run(_build_payload())
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(payload['acquire_rest'])} rest-acquire cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
