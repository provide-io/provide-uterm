#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Isolation + mutation-killing tests for :class:`PresenceManager`.

These exercise the manager against a hand-written fake that implements only
``_PresenceHubCallbacks`` — the point of the structural protocol is that the
manager can be driven without constructing a full ``TermHub``. If the manager
grows a dependency on a hub member outside the protocol, these tests fail to
construct, surfacing the coupling immediately. The assertions also pin every
behavioural branch so they bind PresenceManager's mutmut mutants.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm.server.bridge.hub.presence import PresenceManager


class _FakeRegistry:
    def __init__(self, states: dict[str, Any]) -> None:
        self._states = states

    def get(self, worker_id: str) -> Any:
        return self._states.get(worker_id)


class _FakeHub:
    """Minimal stand-in satisfying ``_PresenceHubCallbacks`` structurally."""

    def __init__(
        self,
        *,
        states: dict[str, Any] | None = None,
        hijacked: bool = False,
        dash_active: bool = False,
        role: str = "viewer",
    ) -> None:
        self._lock = asyncio.Lock()
        self.registry = _FakeRegistry(states or {})
        self._hijacked = hijacked
        self._dash_active = dash_active
        self._role = role
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def is_hijacked(self, st: Any) -> bool:
        # A real hub reads st; pin the arg so None-substitution mutants are killed.
        assert st is not None
        return self._hijacked

    def is_dashboard_hijack_active(self, st: Any) -> bool:
        assert st is not None
        return self._dash_active

    async def _resolve_role_for_browser(self, ws: Any, worker_id: str) -> str:
        assert ws is not None
        assert worker_id
        return self._role

    async def send_worker(self, worker_id: str, msg: dict[str, Any], *, source: Any = None) -> bool:
        self.sent.append((worker_id, msg))
        return True


def _state(
    *, input_mode: str = "open", owner: Any = None, browsers: dict[Any, str] | None = None, online: bool = True
) -> Any:
    return SimpleNamespace(
        input_mode=input_mode,
        hijack_owner=owner,
        browsers=browsers or {},
        worker_ws=object() if online else None,
    )


# -- register_browser_state_snapshot -----------------------------------------


async def test_snapshot_missing_worker_returns_offline_defaults() -> None:
    mgr = PresenceManager(_FakeHub())
    out = await mgr.register_browser_state_snapshot("nope", ws=object())
    assert out == {
        "is_hijacked": False,
        "hijacked_by_me": False,
        "worker_online": False,
        "input_mode": "hijack",
    }


async def test_snapshot_present_worker_hijacked_by_me() -> None:
    ws = object()
    st = _state(input_mode="open", owner=ws, online=True)
    mgr = PresenceManager(_FakeHub(states={"w": st}, hijacked=True, dash_active=True))
    assert await mgr.register_browser_state_snapshot("w", ws=ws) == {
        "is_hijacked": True,
        "hijacked_by_me": True,
        "worker_online": True,
        "input_mode": "open",
    }


async def test_snapshot_hijacked_by_me_false_when_owner_differs() -> None:
    # dash-active but a *different* owner → hijacked_by_me must be False (pins the `and`/`is`).
    st = _state(input_mode="hijack", owner=object(), online=True)
    mgr = PresenceManager(_FakeHub(states={"w": st}, hijacked=True, dash_active=True))
    out = await mgr.register_browser_state_snapshot("w", ws=object())
    assert out["hijacked_by_me"] is False
    assert out["is_hijacked"] is True
    assert out["input_mode"] == "hijack"


async def test_snapshot_worker_online_reflects_worker_ws() -> None:
    st = _state(owner=None, online=False)  # worker_ws is None
    mgr = PresenceManager(_FakeHub(states={"w": st}, hijacked=False, dash_active=False))
    out = await mgr.register_browser_state_snapshot("w", ws=object())
    assert out["worker_online"] is False
    assert out["is_hijacked"] is False
    assert out["hijacked_by_me"] is False


# -- can_send_input ----------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [("operator", True), ("admin", True), ("viewer", False)],
)
def test_can_send_input_open_mode_role_gate(role: str, expected: bool) -> None:
    mgr = PresenceManager(_FakeHub())
    ws = object()
    st = _state(input_mode="open", browsers={ws: role})
    assert mgr.can_send_input(st, ws) is expected


def test_can_send_input_open_mode_unknown_defaults_to_viewer() -> None:
    mgr = PresenceManager(_FakeHub())
    ws = object()
    st = _state(input_mode="open", browsers={})
    assert mgr.can_send_input(st, ws) is False


def test_can_send_input_hijack_owner_only() -> None:
    ws = object()
    mgr_active = PresenceManager(_FakeHub(dash_active=True))
    assert mgr_active.can_send_input(_state(input_mode="hijack", owner=ws), ws) is True
    assert mgr_active.can_send_input(_state(input_mode="hijack", owner=object()), ws) is False
    # Not dashboard-hijack-active → even the owner cannot send (pins the `and`).
    mgr_inactive = PresenceManager(_FakeHub(dash_active=False))
    assert mgr_inactive.can_send_input(_state(input_mode="hijack", owner=ws), ws) is False


# -- resolve_role_for_browser ------------------------------------------------


async def test_resolve_role_delegates_to_callback() -> None:
    mgr = PresenceManager(_FakeHub(role="admin"))
    assert await mgr.resolve_role_for_browser(object(), "w") == "admin"


# -- worker-bound presence control frames ------------------------------------


@pytest.mark.parametrize(
    ("call", "expected_type"),
    [("request_snapshot", "snapshot_req"), ("request_analysis", "analyze_req")],
)
async def test_presence_control_frames_go_to_worker(call: str, expected_type: str) -> None:
    hub = _FakeHub()
    mgr = PresenceManager(hub)
    await getattr(mgr, call)("w")
    assert len(hub.sent) == 1
    worker_id, msg = hub.sent[0]
    assert worker_id == "w"
    assert msg["type"] == expected_type
    # req_id is a real uuid (kills str(None)="None", which is truthy); ts is a clock value.
    assert "-" in msg["req_id"]
    assert msg["ts"] > 0
