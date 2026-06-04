#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Isolation tests for :class:`PresenceManager`.

These exercise the manager against a hand-written fake that implements only
``_PresenceHubCallbacks`` — the whole point of the structural protocol is that
the manager can be driven without constructing a full ``TermHub``. If the
manager grows a dependency on a hub member outside the protocol, these tests
fail to construct, surfacing the coupling immediately.
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
        return self._hijacked

    def is_dashboard_hijack_active(self, st: Any) -> bool:
        return self._dash_active

    async def _resolve_role_for_browser(self, ws: Any, worker_id: str) -> str:
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


async def test_snapshot_missing_worker_returns_offline_defaults() -> None:
    mgr = PresenceManager(_FakeHub())
    out = await mgr.register_browser_state_snapshot("nope", ws=object())
    assert out == {
        "is_hijacked": False,
        "hijacked_by_me": False,
        "worker_online": False,
        "input_mode": "hijack",
    }


async def test_snapshot_reflects_hub_predicates() -> None:
    ws = object()
    st = _state(input_mode="open", owner=ws)
    mgr = PresenceManager(_FakeHub(states={"w": st}, hijacked=True, dash_active=True))
    out = await mgr.register_browser_state_snapshot("w", ws=ws)
    assert out == {
        "is_hijacked": True,
        "hijacked_by_me": True,
        "worker_online": True,
        "input_mode": "open",
    }


def test_can_send_input_open_mode_role_gate() -> None:
    mgr = PresenceManager(_FakeHub())
    ws = object()
    op = _state(input_mode="open", browsers={ws: "operator"})
    viewer = _state(input_mode="open", browsers={ws: "viewer"})
    unknown = _state(input_mode="open", browsers={})
    assert mgr.can_send_input(op, ws) is True
    assert mgr.can_send_input(viewer, ws) is False
    assert mgr.can_send_input(unknown, ws) is False  # defaults to viewer


def test_can_send_input_hijack_owner_only() -> None:
    mgr = PresenceManager(_FakeHub(dash_active=True))
    ws = object()
    owned = _state(input_mode="hijack", owner=ws)
    other = _state(input_mode="hijack", owner=object())
    assert mgr.can_send_input(owned, ws) is True
    assert mgr.can_send_input(other, ws) is False


async def test_resolve_role_delegates_to_callback() -> None:
    mgr = PresenceManager(_FakeHub(role="admin"))
    assert await mgr.resolve_role_for_browser(object(), "w") == "admin"


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
    assert "req_id" in msg and "ts" in msg
