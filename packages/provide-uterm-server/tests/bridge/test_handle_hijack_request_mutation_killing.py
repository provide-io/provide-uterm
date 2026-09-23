#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``browser_handlers._handle_hijack_request``.

Kill-suite only — behavioural coverage of the WebSocket hijack flow lives in
``test_browser_handlers_*.py``. Those suites check the outcome of a request but
not the exact frames, the compensating ``resume`` payload, the metric names or
the event, and their hubs answer regardless of arguments: under mutmut 3.8
(2026-09-23) ``_handle_hijack_request`` had 56 survivors, every one of them on
a failure branch's frames, the conflict metric, the compensating resume, or the
success path's broadcast/metric/notify/event calls.

Everything here drives ``_handle_hijack_request`` directly against a strict hub
that raises on any unexpected argument, with the clock pinned so the resume
payload's ``ts`` is exact.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm.control_channel import encode_control_frame
from provide.uterm.server.bridge.frames import make_error_frame
from provide.uterm.server.bridge.routes import browser_handlers
from provide.uterm.server.bridge.routes.browser_handlers import _handle_hijack_request

WID = "hijack-worker"
NOW = 1000.0
STATE = {"type": "hijack_state", "marker": "distinct"}
PAUSE = {"type": "control", "action": "pause", "owner": "dashboard", "lease_s": 0, "ts": NOW}
RESUME = {"type": "control", "action": "resume", "owner": "dashboard", "lease_s": 0, "ts": NOW}
NO_WORKER = "No worker connected for this session."
ALREADY = "Already hijacked by another client."


class _WS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        assert isinstance(text, str), f"send_text got {text!r}"
        self.sent.append(text)


class _Hub:
    """A hub whose collaborators raise on any unexpected argument."""

    def __init__(
        self,
        ws: _WS,
        *,
        open_mode: bool = False,
        pause_sent: bool = True,
        acquire: tuple[bool, str | None] = (True, None),
    ) -> None:
        self.ws = ws
        self._open_mode = open_mode
        self._pause_sent = pause_sent
        self._acquire = acquire
        self.worker_sends: list[dict[str, Any]] = []
        self.unowned_sends: list[dict[str, Any]] = []
        self.metrics: list[str] = []
        self.broadcasts: list[str] = []
        self.notifications: list[tuple[str, bool, str | None]] = []
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def is_input_open_mode(self, worker_id: str) -> bool:
        assert worker_id == WID, f"is_input_open_mode got {worker_id!r}"
        return self._open_mode

    async def send_worker(self, worker_id: str, msg: dict[str, Any]) -> bool:
        assert worker_id == WID, f"send_worker got {worker_id!r}"
        self.worker_sends.append(msg)
        return self._pause_sent

    async def try_acquire_ws_hijack(self, worker_id: str, ws: Any) -> tuple[bool, str | None]:
        assert (worker_id, ws) == (WID, self.ws), f"try_acquire_ws_hijack got {(worker_id, ws)!r}"
        return self._acquire

    async def send_worker_if_unowned(self, worker_id: str, msg: dict[str, Any]) -> bool:
        assert worker_id == WID, f"send_worker_if_unowned got {worker_id!r}"
        assert isinstance(msg, dict), f"send_worker_if_unowned got {msg!r}"
        self.unowned_sends.append(msg)
        return True

    async def hijack_state_msg_for(self, worker_id: str, ws: Any) -> dict[str, Any]:
        assert (worker_id, ws) == (WID, self.ws), f"hijack_state_msg_for got {(worker_id, ws)!r}"
        return dict(STATE)

    def metric(self, name: str) -> None:
        self.metrics.append(name)

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        self.broadcasts.append(worker_id)

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None) -> None:
        self.notifications.append((worker_id, enabled, owner))

    async def append_event(self, worker_id: str, event: str, payload: dict[str, Any]) -> None:
        self.events.append((worker_id, event, payload))


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_handlers, "time", SimpleNamespace(time=lambda: NOW))


def _error(text: str) -> str:
    return encode_control_frame(make_error_frame(text))


STATE_FRAME = encode_control_frame(STATE)


async def test_a_worker_that_cannot_be_paused_gets_the_error_and_the_state() -> None:
    """Kills mutmut_46 (the state frame replaced by None) and 48/49 (its
    ``hijack_state_msg_for`` arguments). Ownership is untouched."""
    ws = _WS()
    hub = _Hub(ws, pause_sent=False)
    assert await _handle_hijack_request(hub, ws, WID, "admin", owned_hijack=False) is False
    assert ws.sent == [_error(NO_WORKER), STATE_FRAME]
    assert hub.worker_sends == [PAUSE]
    assert (hub.unowned_sends, hub.metrics, hub.events) == ([], [], [])


async def test_a_lost_race_counts_a_conflict_and_does_not_resume_the_owner() -> None:
    """Kills mutmut_58-66: ``already_hijacked`` counts one
    ``hijack_conflicts_total`` and sends no compensating resume (that would
    unpause the legitimate owner). Also mutmut_89 and 105/106 on this branch."""
    ws = _WS()
    hub = _Hub(ws, acquire=(False, "already_hijacked"))
    assert await _handle_hijack_request(hub, ws, WID, "admin", owned_hijack=False) is False
    assert hub.metrics == ["hijack_conflicts_total"]
    assert hub.unowned_sends == []
    assert ws.sent == [_error(ALREADY), STATE_FRAME]


async def test_a_vanished_worker_is_resumed_and_reported_as_missing() -> None:
    """Kills mutmut_64-66 (the resume guard), 67-87 (the exact compensating
    resume: worker id, every key and value, ``lease_s`` 0 and ``ts``) and
    89-96 (``no_worker`` picks the no-worker text)."""
    ws = _WS()
    hub = _Hub(ws, acquire=(False, "no_worker"))
    assert await _handle_hijack_request(hub, ws, WID, "admin", owned_hijack=False) is False
    assert hub.unowned_sends == [RESUME]
    assert hub.metrics == []
    assert ws.sent == [_error(NO_WORKER), STATE_FRAME]


async def test_any_other_acquire_failure_is_resumed_and_reported_as_taken() -> None:
    """The other side of the message choice: an error that is neither
    ``no_worker`` nor ``already_hijacked`` resumes the worker and says it is
    taken, without counting a conflict."""
    ws = _WS()
    hub = _Hub(ws, acquire=(False, "rest_lease_active"))
    assert await _handle_hijack_request(hub, ws, WID, "admin", owned_hijack=False) is False
    assert hub.unowned_sends == [RESUME]
    assert hub.metrics == []
    assert ws.sent == [_error(ALREADY), STATE_FRAME]


@pytest.mark.parametrize("owned", [True, False])
async def test_a_failed_acquire_leaves_ownership_as_it_was(owned: bool) -> None:
    """A failure path returns the caller's ``owned_hijack`` unchanged, in
    either direction."""
    ws = _WS()
    hub = _Hub(ws, acquire=(False, "already_hijacked"))
    assert await _handle_hijack_request(hub, ws, WID, "admin", owned_hijack=owned) is owned


async def test_a_successful_acquire_broadcasts_counts_notifies_and_records() -> None:
    """Kills mutmut_109 (broadcast worker id), 110-112 (the acquire metric),
    113 (notify worker id) and 122-133 (the exact ``hijack_acquired`` event).
    """
    ws = _WS()
    hub = _Hub(ws)
    assert await _handle_hijack_request(hub, ws, WID, "admin", owned_hijack=False) is True
    assert hub.worker_sends == [PAUSE]
    assert hub.broadcasts == [WID]
    assert hub.metrics == ["hijack_acquires_total"]
    assert hub.notifications == [(WID, True, "dashboard")]
    assert hub.events == [(WID, "hijack_acquired", {"owner": "dashboard_ws"})]
    assert (ws.sent, hub.unowned_sends) == ([], [])


async def test_a_non_admin_is_refused_before_the_worker_is_touched() -> None:
    ws = _WS()
    hub = _Hub(ws)
    assert await _handle_hijack_request(hub, ws, WID, "operator", owned_hijack=True) is True
    assert ws.sent == [_error("Hijack requires admin role.")]
    assert hub.worker_sends == []


async def test_open_input_mode_refuses_a_hijack() -> None:
    ws = _WS()
    hub = _Hub(ws, open_mode=True)
    assert await _handle_hijack_request(hub, ws, WID, "admin", owned_hijack=False) is False
    assert ws.sent == [_error("Hijack not available in open input mode.")]
    assert hub.worker_sends == []
