#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``rest._hijack_heartbeat``, ``rest._hijack_snapshot``,
``rest._hijack_events`` and ``rest.register_rest_routes``.

Kill-suite only -- like the other seven route bodies in this module, these four
were pulled out of ``@router.*``-decorated closures specifically because mutmut
skips decorated functions outright (see the module docstring in ``rest.py`` and
docs/mutmut-survivors-triage.md Wave 9). No existing test drives these bodies
with a hub strict enough to notice an argument silently replaced with ``None``,
a JSON error literal mangled, or a wall-clock conversion fed the wrong monotonic
value -- which is exactly the operator set mutmut's standard mutators generate
against this source: ``None``-substitution for every argument passed to a hub
collaborator or to ``build_hijack_snapshot_response``/``build_hijack_events_response``,
string-literal mangling ("XX..XX" wrapping and case flips) in the returned
error bodies and in the ``hijack_heartbeat`` event type/dict keys, a dropped
keyword argument (which silently falls back to the collaborator's own default),
and the ``and``/``or`` and ``not``-removal flips on the ``isinstance(served, int)
and not isinstance(served, bool)`` guard in ``_hijack_snapshot``.

Every hub collaborator below is a strict fake asserting the exact positional/
keyword arguments it receives, mirroring the real ``TermHub`` signatures in
``packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/core_impl.py``:
``get_rest_session(worker_id, hijack_id)``, ``clamp_lease(lease_s)``,
``extend_hijack_lease(worker_id, hijack_id, owner, lease_s, now)``,
``append_event(worker_id, event_type, data=None)``, ``broadcast_hijack_state(worker_id)``,
``wait_for_snapshot(worker_id, timeout_ms=1500, *, after_event_seq=None)``,
``get_fresh_hijack_expiry(worker_id, hijack_id, fallback)`` and
``get_hijack_events_data(worker_id, hijack_id, hs, after_seq, limit)``. None of
these fakes use ``AsyncMock(return_value=...)``: a mock configured that way
still returns a value after an argument is silently dropped or replaced with
``None``, so it cannot distinguish the real call from a mutant's call. A plain
assert on each received argument can. ``_hijack_snapshot``/``_hijack_events``
delegate their response-shaping to ``build_hijack_snapshot_response``/
``build_hijack_events_response`` (pure functions in ``rest_helpers.py``, out of
scope here); both are monkeypatched in ``rest`` with strict recorders that
assert every keyword argument threaded through, rather than calling the real
builder, so a test failure points at the delegation site in ``rest.py`` and
not at ``rest_helpers.py``.

``rest.time`` is monkeypatched module-wide to a fixed ``(NOW, MONO)`` pair so
``_mono_to_wall``'s wall-clock conversion is an exact, reproducible number in
every assertion rather than a moving target.

Documented equivalents: none. Every surviving mutant in the 61-item list either
swaps a value threaded through a hub call or response-builder call, mangles a
JSON error literal, mangles a dict/event-type string, or flips a branch
condition on the ``isinstance`` guard -- all observable and pinned by an exact
equality assertion below.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from provide.uterm.bridge.coordinator import HijackSession
from provide.uterm.server.bridge.models import HijackHeartbeatRequest
from provide.uterm.server.bridge.routes import rest
from provide.uterm.server.bridge.routes.rest import (
    _hijack_events,
    _hijack_heartbeat,
    _hijack_snapshot,
    register_rest_routes,
)

WID = "w1"
HID = "h1"

NOW = 1000.0
MONO = 100.0


class _Clock:
    """Stand-in for the ``time`` module: fixed wall clock and monotonic clock."""

    @staticmethod
    def time() -> float:
        return NOW

    @staticmethod
    def monotonic() -> float:
        return MONO


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``rest.time`` so every ``_mono_to_wall`` conversion is an exact number."""
    monkeypatch.setattr(rest, "time", _Clock)


async def _run(coro: Any) -> Any:
    """Run a coroutine with a hard timeout so a mutant that hangs cannot wedge the suite."""
    return await asyncio.wait_for(coro, 2)


def _assert_invalid_session_404(resp: Any) -> None:
    assert isinstance(resp, JSONResponse), f"expected a JSONResponse, got {resp!r}"
    assert resp.status_code == 404
    assert json.loads(resp.body) == {"error": "Invalid or expired hijack session."}


# ---------------------------------------------------------------------------
# _hijack_heartbeat
# ---------------------------------------------------------------------------


class _HeartbeatMissingSessionHub:
    """Only ``get_rest_session`` is defined. If a mutant reaches any further
    hub call, the missing attribute raises ``AttributeError`` and fails the
    test just as surely as a mismatched-argument assertion would."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> None:
        assert worker_id == WID, f"get_rest_session got worker_id={worker_id!r}"
        assert hijack_id == HID, f"get_rest_session got hijack_id={hijack_id!r}"
        self.calls.append((worker_id, hijack_id))
        return


async def test_heartbeat_missing_session_returns_exact_404() -> None:
    """First ``hs is None`` guard: kills mutmut_10 (the error dict replaced
    with ``None``, which ``JSONResponse`` renders as JSON ``null`` -- not a
    dict) and mutmut_14/15/16/17/18 (the ``"error"`` key or the message
    mangled/case-flipped). Only ``get_rest_session`` is exercised; reaching
    ``clamp_lease``/``extend_hijack_lease`` would raise ``AttributeError``."""
    hub = _HeartbeatMissingSessionHub()
    req = HijackHeartbeatRequest(lease_s=90)
    resp = await _run(_hijack_heartbeat(hub, WID, HID, req))
    _assert_invalid_session_404(resp)
    assert hub.calls == [(WID, HID)]


class _HeartbeatExtendFailsHub:
    """``get_rest_session`` and ``clamp_lease`` succeed; ``extend_hijack_lease``
    returns ``None`` (the lease was concurrently released/expired)."""

    def __init__(self, *, hs: HijackSession, lease_s_in: int, lease_s_out: int) -> None:
        self._hs = hs
        self._lease_s_in = lease_s_in
        self._lease_s_out = lease_s_out
        self.extend_calls: list[tuple[str, str, str, int, float]] = []

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> HijackSession:
        assert (worker_id, hijack_id) == (WID, HID)
        return self._hs

    def clamp_lease(self, lease_s: int) -> int:
        assert lease_s == self._lease_s_in, f"clamp_lease got lease_s={lease_s!r}"
        return self._lease_s_out

    async def extend_hijack_lease(self, worker_id: str, hijack_id: str, owner: str, lease_s: int, now: float) -> None:
        assert worker_id == WID, f"extend_hijack_lease got worker_id={worker_id!r}"
        assert hijack_id == HID, f"extend_hijack_lease got hijack_id={hijack_id!r}"
        assert owner == self._hs.owner, f"extend_hijack_lease got owner={owner!r}"
        assert lease_s == self._lease_s_out, f"extend_hijack_lease got lease_s={lease_s!r}"
        assert now == MONO, f"extend_hijack_lease got now={now!r}"
        self.extend_calls.append((worker_id, hijack_id, owner, lease_s, now))
        return


async def test_heartbeat_expired_lease_returns_exact_404() -> None:
    """Second ``new_expires is None`` guard (a distinct code location from the
    missing-session guard, sharing the same literal): kills mutmut_41 (the
    message mangled to ``"XXInvalid or expired hijack session.XX"``). Reaching
    ``append_event``/``broadcast_hijack_state`` would raise ``AttributeError``
    since this hub does not define them."""
    hs = HijackSession(hijack_id=HID, owner="alice", lease_expires_at=500.0)
    hub = _HeartbeatExtendFailsHub(hs=hs, lease_s_in=555, lease_s_out=300)
    req = HijackHeartbeatRequest(lease_s=555)
    resp = await _run(_hijack_heartbeat(hub, WID, HID, req))
    _assert_invalid_session_404(resp)
    assert hub.extend_calls == [(WID, HID, "alice", 300, MONO)]


class _HeartbeatHub(_HeartbeatExtendFailsHub):
    """Full happy path: ``extend_hijack_lease`` succeeds, then
    ``append_event``/``broadcast_hijack_state`` fire."""

    def __init__(self, *, hs: HijackSession, lease_s_in: int, lease_s_out: int, new_expires: float) -> None:
        super().__init__(hs=hs, lease_s_in=lease_s_in, lease_s_out=lease_s_out)
        self._new_expires = new_expires
        self.append_calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.broadcast_calls: list[str] = []

    async def extend_hijack_lease(self, worker_id: str, hijack_id: str, owner: str, lease_s: int, now: float) -> float:
        await super().extend_hijack_lease(worker_id, hijack_id, owner, lease_s, now)
        return self._new_expires

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> None:
        assert worker_id == WID, f"append_event got worker_id={worker_id!r}"
        assert event_type == "hijack_heartbeat", f"append_event got event_type={event_type!r}"
        assert data == {"hijack_id": HID, "lease_s": self._lease_s_out}, f"append_event got data={data!r}"
        self.append_calls.append((worker_id, event_type, data))

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        assert worker_id == WID, f"broadcast_hijack_state got {worker_id!r}"
        self.broadcast_calls.append(worker_id)


async def test_heartbeat_success_appends_event_broadcasts_and_returns_exact_payload() -> None:
    """Happy path: kills mutmut_45/46/47/50 (``None``-substituted/dropped
    arguments on the ``append_event(worker_id, "hijack_heartbeat", {...})``
    call -- the strict fake asserts ``data`` by exact dict equality, so a
    dropped kwarg falling back to the collaborator's own ``data=None`` default
    is caught the same way as an explicit ``None``), mutmut_51/52 (the event
    type string mangled/case-flipped), mutmut_53/54/55/56 (the ``"hijack_id"``/
    ``"lease_s"`` dict keys mangled/case-flipped), mutmut_57 (``broadcast_hijack_state``
    called with ``None`` instead of ``worker_id``), and mutmut_61/62 (the
    returned dict's ``"worker_id"`` key mangled/case-flipped -- caught by the
    exact ``==`` on the whole payload)."""
    hs = HijackSession(hijack_id=HID, owner="alice", lease_expires_at=500.0)
    hub = _HeartbeatHub(hs=hs, lease_s_in=555, lease_s_out=300, new_expires=130.0)
    req = HijackHeartbeatRequest(lease_s=555)
    result = await _run(_hijack_heartbeat(hub, WID, HID, req))
    assert result == {
        "ok": True,
        "worker_id": WID,
        "hijack_id": HID,
        "lease_expires_at": 1030.0,  # _mono_to_wall(130.0) == NOW + (130.0 - MONO)
    }
    assert hub.append_calls == [(WID, "hijack_heartbeat", {"hijack_id": HID, "lease_s": 300})]
    assert hub.broadcast_calls == [WID]


# ---------------------------------------------------------------------------
# _hijack_snapshot
# ---------------------------------------------------------------------------


class _SnapshotMissingSessionHub:
    """Only ``get_rest_session`` is defined."""

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> None:
        assert (worker_id, hijack_id) == (WID, HID)
        return


async def test_snapshot_missing_session_returns_exact_404() -> None:
    """Kills mutmut_7 (error body replaced with ``None``) and mutmut_11-15
    (the ``"error"`` key or message mangled/case-flipped)."""
    resp = await _run(_hijack_snapshot(_SnapshotMissingSessionHub(), WID, HID, 1500))
    _assert_invalid_session_404(resp)


class _SnapshotHub:
    """Strict fake mirroring ``get_rest_session``/``wait_for_snapshot``/
    ``get_fresh_hijack_expiry``."""

    def __init__(
        self,
        *,
        hs: HijackSession,
        timeout_ms: int,
        after_event_seq: int,
        snapshot_result: dict[str, Any] | None,
        fresh_result: float,
    ) -> None:
        self._hs = hs
        self._timeout_ms = timeout_ms
        self._after_event_seq = after_event_seq
        self._snapshot_result = snapshot_result
        self._fresh_result = fresh_result
        self.wait_calls: list[tuple[str, int, int]] = []
        self.fresh_calls: list[tuple[str, str, float]] = []

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> HijackSession:
        assert (worker_id, hijack_id) == (WID, HID)
        return self._hs

    async def wait_for_snapshot(
        self, worker_id: str, timeout_ms: int = 1500, *, after_event_seq: int | None = None
    ) -> dict[str, Any] | None:
        assert worker_id == WID, f"wait_for_snapshot got worker_id={worker_id!r}"
        assert timeout_ms == self._timeout_ms, f"wait_for_snapshot got timeout_ms={timeout_ms!r}"
        assert after_event_seq == self._after_event_seq, f"wait_for_snapshot got after_event_seq={after_event_seq!r}"
        self.wait_calls.append((worker_id, timeout_ms, after_event_seq))
        return self._snapshot_result

    async def get_fresh_hijack_expiry(self, worker_id: str, hijack_id: str, fallback: float) -> float:
        assert worker_id == WID, f"get_fresh_hijack_expiry got worker_id={worker_id!r}"
        assert hijack_id == HID, f"get_fresh_hijack_expiry got hijack_id={hijack_id!r}"
        assert fallback == self._hs.lease_expires_at, f"get_fresh_hijack_expiry got fallback={fallback!r}"
        self.fresh_calls.append((worker_id, hijack_id, fallback))
        return self._fresh_result


async def test_snapshot_updates_served_seq_and_builds_exact_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path with a real, non-bool ``event_seq``: kills mutmut_19/20
    (``timeout_ms``/``after_event_seq`` replaced with ``None`` on the
    ``wait_for_snapshot`` call), mutmut_22/23 (the ``timeout_ms``/
    ``after_event_seq`` kwarg dropped entirely, silently falling back to the
    collaborator's own default of ``1500``/``None`` -- caught because both
    values here are deliberately non-default), mutmut_24 (``if snapshot is not
    None`` inverted to ``is None``, which would skip the update entirely),
    mutmut_25/26/27/28 (``served`` forced to ``None``/looked up under the
    wrong key, each of which makes ``snapshot.get(...)`` return ``None`` since
    the real key is absent), mutmut_30 (``and not isinstance(served, bool)``
    weakened to ``and isinstance(served, bool)``, which is False for a real
    int and would also skip the update), mutmut_31 (``hs.last_served_event_seq``
    set to ``None`` instead of ``served``), mutmut_33/34 (``worker_id``/
    ``hijack_id`` replaced with ``None`` on the ``get_fresh_hijack_expiry``
    call), and mutmut_39/40/41 (``worker_id``/``hijack_id``/``snapshot``
    replaced with ``None`` on the ``build_hijack_snapshot_response`` call --
    caught by the strict recorder below)."""
    calls: list[dict[str, Any]] = []

    def fake_builder(*, worker_id: str, hijack_id: str, snapshot: Any, lease_expires_at: float | None) -> Any:
        calls.append(
            {
                "worker_id": worker_id,
                "hijack_id": hijack_id,
                "snapshot": snapshot,
                "lease_expires_at": lease_expires_at,
            }
        )
        return {"sentinel": "snapshot_response"}

    monkeypatch.setattr(rest, "build_hijack_snapshot_response", fake_builder)

    hs = HijackSession(hijack_id=HID, owner="alice", lease_expires_at=500.0, last_served_event_seq=5)
    snapshot = {"event_seq": 42}
    hub = _SnapshotHub(hs=hs, timeout_ms=777, after_event_seq=5, snapshot_result=snapshot, fresh_result=150.0)

    result = await _run(_hijack_snapshot(hub, WID, HID, 777))

    assert hs.last_served_event_seq == 42
    assert hub.wait_calls == [(WID, 777, 5)]
    assert hub.fresh_calls == [(WID, HID, 500.0)]
    assert calls == [
        {
            "worker_id": WID,
            "hijack_id": HID,
            "snapshot": snapshot,
            "lease_expires_at": 1050.0,  # _mono_to_wall(150.0) == NOW + (150.0 - MONO)
        }
    ]
    assert result == {"sentinel": "snapshot_response"}


async def test_snapshot_bool_event_seq_is_never_adopted_as_the_served_seq(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutmut_29: ``isinstance(served, int) and not isinstance(served,
    bool)`` weakened to ``isinstance(served, int) or not isinstance(served,
    bool)``. ``bool`` is a subclass of ``int``, so with ``served = True`` the
    real guard is ``True and not True`` == ``False`` (booleans are never
    adopted as an event sequence number) while the mutant's ``or`` makes it
    ``True or False`` == ``True`` and wrongly adopts it."""
    monkeypatch.setattr(rest, "build_hijack_snapshot_response", lambda **_kw: {"sentinel": "snapshot_response"})
    hs = HijackSession(hijack_id=HID, owner="alice", lease_expires_at=500.0, last_served_event_seq=5)
    snapshot = {"event_seq": True}
    hub = _SnapshotHub(hs=hs, timeout_ms=1500, after_event_seq=5, snapshot_result=snapshot, fresh_result=150.0)
    await _run(_hijack_snapshot(hub, WID, HID, 1500))
    assert hs.last_served_event_seq == 5


# ---------------------------------------------------------------------------
# _hijack_events
# ---------------------------------------------------------------------------


class _EventsMissingSessionHub:
    """Only ``get_rest_session`` is defined."""

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> None:
        assert (worker_id, hijack_id) == (WID, HID)
        return


async def test_events_missing_session_returns_exact_404() -> None:
    """Kills mutmut_7 (error body replaced with ``None``) and mutmut_11-15
    (the ``"error"`` key or message mangled/case-flipped)."""
    resp = await _run(_hijack_events(_EventsMissingSessionHub(), WID, HID, 0, 200))
    _assert_invalid_session_404(resp)


class _EventsHub:
    """Strict fake mirroring ``get_rest_session``/``get_hijack_events_data``."""

    def __init__(self, *, hs: HijackSession, after_seq: int, limit: int, events_data: dict[str, Any]) -> None:
        self._hs = hs
        self._after_seq = after_seq
        self._limit = limit
        self._events_data = events_data
        self.calls: list[tuple[str, str, Any, int, int]] = []

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> HijackSession:
        assert (worker_id, hijack_id) == (WID, HID)
        return self._hs

    async def get_hijack_events_data(
        self, worker_id: str, hijack_id: str, hs: Any, after_seq: int, limit: int
    ) -> dict[str, Any]:
        assert worker_id == WID, f"get_hijack_events_data got worker_id={worker_id!r}"
        assert hijack_id == HID, f"get_hijack_events_data got hijack_id={hijack_id!r}"
        assert hs is self._hs, f"get_hijack_events_data got hs={hs!r}"
        assert after_seq == self._after_seq, f"get_hijack_events_data got after_seq={after_seq!r}"
        assert limit == self._limit, f"get_hijack_events_data got limit={limit!r}"
        self.calls.append((worker_id, hijack_id, hs, after_seq, limit))
        return self._events_data


async def test_events_builds_exact_response_from_events_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutmut_18-22 (each of ``worker_id``/``hijack_id``/``hs``/
    ``after_seq``/``limit`` replaced with ``None`` on the
    ``get_hijack_events_data`` call -- the strict fake asserts every one),
    mutmut_31/34 (``latest_seq``/``min_event_seq`` forced to ``None`` instead
    of read from ``events_data``), and mutmut_42/43/44/47 (``after_seq``/
    ``latest_seq``/``min_event_seq``/``lease_expires_at`` replaced with
    ``None`` on the ``build_hijack_events_response`` call -- caught by the
    strict recorder below). Distinct values for every field guard against a
    swap between them."""
    calls: list[dict[str, Any]] = []

    def fake_builder(
        *,
        worker_id: str,
        hijack_id: str,
        after_seq: int,
        latest_seq: int,
        min_event_seq: int,
        events: list[Any],
        limit: int,
        lease_expires_at: float | None,
    ) -> Any:
        calls.append(
            {
                "worker_id": worker_id,
                "hijack_id": hijack_id,
                "after_seq": after_seq,
                "latest_seq": latest_seq,
                "min_event_seq": min_event_seq,
                "events": events,
                "limit": limit,
                "lease_expires_at": lease_expires_at,
            }
        )
        return {"sentinel": "events_response"}

    monkeypatch.setattr(rest, "build_hijack_events_response", fake_builder)

    hs = HijackSession(hijack_id=HID, owner="alice", lease_expires_at=500.0)
    events_data = {"rows": ["e1", "e2"], "latest_seq": 42, "min_event_seq": 5, "fresh_expires": 150.0}
    hub = _EventsHub(hs=hs, after_seq=7, limit=55, events_data=events_data)

    result = await _run(_hijack_events(hub, WID, HID, 7, 55))

    assert hub.calls == [(WID, HID, hs, 7, 55)]
    assert calls == [
        {
            "worker_id": WID,
            "hijack_id": HID,
            "after_seq": 7,
            "latest_seq": 42,
            "min_event_seq": 5,
            "events": ["e1", "e2"],
            "limit": 55,
            "lease_expires_at": 1050.0,  # _mono_to_wall(150.0) == NOW + (150.0 - MONO)
        }
    ]
    assert result == {"sentinel": "events_response"}


# ---------------------------------------------------------------------------
# register_rest_routes
# ---------------------------------------------------------------------------


async def test_register_rest_routes_registers_exact_routes_and_forwards_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutmut_9: ``register_gui_vnc_ws_routes(hub, router)`` mangled to
    ``register_gui_vnc_ws_routes(None, router)``. Also pins the exact set of
    seven hijack routes ``register_rest_routes`` attaches directly (path +
    HTTP method), that all three sibling registrars (``register_workerctl_routes``,
    ``register_gui_routes``, ``register_gui_vnc_ws_routes``) are forwarded the
    exact ``(hub, router)`` pair, and that the registered heartbeat/snapshot/
    events endpoints delegate to the matching module-level ``_hijack_*``
    function with exactly the arguments FastAPI would have extracted from the
    path/query parameters -- independent of those functions' own bodies, which
    are monkeypatched out here."""
    sibling_calls: dict[str, list[tuple[Any, APIRouter]]] = {"workerctl": [], "gui": [], "vnc": []}

    def _recorder(name: str) -> Any:
        def _fn(hub: Any, router: APIRouter) -> None:
            sibling_calls[name].append((hub, router))

        return _fn

    monkeypatch.setattr(rest, "register_workerctl_routes", _recorder("workerctl"))
    monkeypatch.setattr(rest, "register_gui_routes", _recorder("gui"))
    monkeypatch.setattr(rest, "register_gui_vnc_ws_routes", _recorder("vnc"))

    heartbeat_calls: list[tuple[Any, str, str, Any]] = []
    snapshot_calls: list[tuple[Any, str, str, int]] = []
    events_calls: list[tuple[Any, str, str, int, int]] = []

    async def fake_heartbeat(hub: Any, worker_id: str, hijack_id: str, request: Any) -> Any:
        heartbeat_calls.append((hub, worker_id, hijack_id, request))
        return {"ok": "heartbeat"}

    async def fake_snapshot(hub: Any, worker_id: str, hijack_id: str, wait_ms: int) -> Any:
        snapshot_calls.append((hub, worker_id, hijack_id, wait_ms))
        return {"ok": "snapshot"}

    async def fake_events(hub: Any, worker_id: str, hijack_id: str, after_seq: int, limit: int) -> Any:
        events_calls.append((hub, worker_id, hijack_id, after_seq, limit))
        return {"ok": "events"}

    monkeypatch.setattr(rest, "_hijack_heartbeat", fake_heartbeat)
    monkeypatch.setattr(rest, "_hijack_snapshot", fake_snapshot)
    monkeypatch.setattr(rest, "_hijack_events", fake_events)

    sentinel_hub = object()
    router = APIRouter()
    register_rest_routes(sentinel_hub, router)

    routes_by_path = {r.path: r for r in router.routes}
    assert set(routes_by_path) == {
        "/worker/{worker_id}/hijack/acquire",
        "/worker/{worker_id}/hijack/{hijack_id}/heartbeat",
        "/worker/{worker_id}/hijack/{hijack_id}/snapshot",
        "/worker/{worker_id}/hijack/{hijack_id}/events",
        "/worker/{worker_id}/hijack/{hijack_id}/send",
        "/worker/{worker_id}/hijack/{hijack_id}/step",
        "/worker/{worker_id}/hijack/{hijack_id}/release",
    }
    assert routes_by_path["/worker/{worker_id}/hijack/{hijack_id}/heartbeat"].methods == {"POST"}
    assert routes_by_path["/worker/{worker_id}/hijack/{hijack_id}/snapshot"].methods == {"GET"}
    assert routes_by_path["/worker/{worker_id}/hijack/{hijack_id}/events"].methods == {"GET"}

    assert sibling_calls == {
        "workerctl": [(sentinel_hub, router)],
        "gui": [(sentinel_hub, router)],
        "vnc": [(sentinel_hub, router)],
    }

    heartbeat_endpoint = routes_by_path["/worker/{worker_id}/hijack/{hijack_id}/heartbeat"].endpoint
    req = HijackHeartbeatRequest(lease_s=42)
    result = await _run(heartbeat_endpoint(worker_id="w1", hijack_id="h1", request=req))
    assert heartbeat_calls == [(sentinel_hub, "w1", "h1", req)]
    assert result == {"ok": "heartbeat"}

    snapshot_endpoint = routes_by_path["/worker/{worker_id}/hijack/{hijack_id}/snapshot"].endpoint
    result = await _run(snapshot_endpoint(worker_id="w1", hijack_id="h1", wait_ms=777))
    assert snapshot_calls == [(sentinel_hub, "w1", "h1", 777)]
    assert result == {"ok": "snapshot"}

    events_endpoint = routes_by_path["/worker/{worker_id}/hijack/{hijack_id}/events"].endpoint
    result = await _run(events_endpoint(worker_id="w1", hijack_id="h1", after_seq=3, limit=9))
    assert events_calls == [(sentinel_hub, "w1", "h1", 3, 9)]
    assert result == {"ok": "events"}
