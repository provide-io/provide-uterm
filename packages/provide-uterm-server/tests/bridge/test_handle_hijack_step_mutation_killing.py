#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``browser_handlers._handle_hijack_step``.

Kill-suite only — behavioural coverage of the WebSocket hijack-step flow
lives in ``test_browser_handlers_*.py``. Those suites use hubs (typically
``AsyncMock``-based) that answer the same way regardless of what they were
called with and never assert the exact step-control payload, the
``ownership_generation`` kwarg, the metric name, or the ``append_event``
arguments. That leaves 31 surviving mutants of ``_handle_hijack_step``
(enumerated in the diffs handed to this suite) covering: every key and value
of the ``{"type": "control", "action": "step", ...}`` frame, the
``ownership_generation`` kwarg being forced to ``None`` or dropped entirely,
the ``if not ok`` branch being flipped to ``if ok``, the exact "No worker
connected for this session." error text, the ``"hijack_steps_total"`` metric
name, and every argument (including the omitted/defaulted payload) of the
``append_event(worker_id, "hijack_step", {"owner": "dashboard_ws"})`` call.

Everything here drives ``_handle_hijack_step`` directly against a strict hub
that asserts on every argument it receives, with the clock pinned so the
``ts`` in the step frame is exact and comparisons against the encoded
control frame are exact strings.

Documented equivalents: none. Every one of the 31 survivors changes either
the wire payload sent to the worker, the exact ownership generation
revalidated, the branch taken on success/failure, the text reported to the
browser, or an argument recorded via a hub call — all directly observable
through the fakes below.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm.control_channel import encode_control_frame
from provide.uterm.server.bridge.frames import make_error_frame
from provide.uterm.server.bridge.routes import browser_handlers
from provide.uterm.server.bridge.routes.browser_handlers import _handle_hijack_step

WID = "hijack-worker"
NOW = 1000.0
GENERATION = 7
STEP_MSG = {"type": "control", "action": "step", "owner": "dashboard", "lease_s": 0, "ts": NOW}
NO_WORKER = "No worker connected for this session."


class _WS:
    """A browser socket that records every frame sent to it."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        assert isinstance(text, str), f"send_text got {text!r}"
        self.sent.append(text)


class _Hub:
    """A hub whose collaborators assert on every argument they receive."""

    def __init__(
        self,
        ws: _WS,
        *,
        generation: int | None,
        send_result: tuple[bool, str | None] = (True, None),
    ) -> None:
        self.ws = ws
        self._generation = generation
        self._send_result = send_result
        self.send_calls: list[dict[str, Any]] = []
        self.metrics: list[str] = []
        self.events: list[tuple[str, str, dict[str, Any] | None]] = []

    async def capture_dashboard_ownership(self, worker_id: str, ws: Any) -> int | None:
        assert (worker_id, ws) == (WID, self.ws), f"capture_dashboard_ownership got {(worker_id, ws)!r}"
        return self._generation

    async def send_owned_worker(
        self,
        worker_id: str,
        msg: dict[str, Any],
        *,
        browser_ws: Any = None,
        rest_hijack_id: str | None = None,
        ownership_generation: int | None = None,
        source: Any = None,
    ) -> tuple[bool, str | None]:
        assert worker_id == WID, f"send_owned_worker got worker_id={worker_id!r}"
        assert msg == STEP_MSG, f"send_owned_worker got msg={msg!r}"
        assert browser_ws is self.ws, f"send_owned_worker got browser_ws={browser_ws!r}"
        assert rest_hijack_id is None, f"send_owned_worker got rest_hijack_id={rest_hijack_id!r}"
        assert ownership_generation is not None and ownership_generation == self._generation, (
            f"send_owned_worker got ownership_generation={ownership_generation!r}, want {self._generation!r}"
        )
        assert source is None, f"send_owned_worker got source={source!r}"
        self.send_calls.append(msg)
        return self._send_result

    def metric(self, name: str, value: int = 1) -> None:
        assert value == 1, f"metric got value={value!r}"
        self.metrics.append(name)

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.events.append((worker_id, event_type, data))
        return {}


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_handlers, "time", SimpleNamespace(time=lambda: NOW))


def _error(text: str) -> str:
    return encode_control_frame(make_error_frame(text))


async def test_not_owner_short_circuits_without_touching_the_worker() -> None:
    """When ``capture_dashboard_ownership`` returns ``None`` (this browser
    does not hold the dashboard lease), nothing else runs: no send, no
    frame to the browser, no metric, no event."""
    ws = _WS()
    hub = _Hub(ws, generation=None)
    await _handle_hijack_step(hub, ws, WID)
    assert ws.sent == []
    assert hub.send_calls == []
    assert hub.metrics == []
    assert hub.events == []


async def test_owner_success_sends_exact_step_frame_and_records_metric_and_event() -> None:
    """Kills mutmut_11/15 (``ownership_generation`` forced to ``None`` or
    dropped from the call — caught because the fake asserts it equals the
    exact generation ``capture_dashboard_ownership`` returned), 24-32 (every
    key and value of the step control frame: ``type``/``action``/``owner``/
    ``lease_s``/``ts`` mangled, cased, or the ``lease_s`` value changed),
    33 (the ``if not ok`` branch flipped to ``if ok`` — success must send
    nothing to the browser and must record the metric and event, so a flip
    would send an error and record nothing), 40-42 (the
    ``"hijack_steps_total"`` metric name mangled, cased, or dropped), and
    43-45/48-54 (every argument of the ``append_event`` call, including
    mutmut_48's omitted third argument, which would default to ``None``
    instead of the exact ``{"owner": "dashboard_ws"}`` payload)."""
    ws = _WS()
    hub = _Hub(ws, generation=GENERATION, send_result=(True, None))
    await _handle_hijack_step(hub, ws, WID)
    assert hub.send_calls == [STEP_MSG]
    assert ws.sent == []
    assert hub.metrics == ["hijack_steps_total"]
    assert hub.events == [(WID, "hijack_step", {"owner": "dashboard_ws"})]


async def test_owner_failure_sends_exact_error_and_records_nothing() -> None:
    """Kills mutmut_33 from the other direction (a flipped ``if ok`` would
    record the metric/event instead of reporting the error) and 34-39 (the
    error frame built from ``None``, a mangled literal, or the wrong case
    of "No worker connected for this session." instead of the exact text)."""
    ws = _WS()
    hub = _Hub(ws, generation=GENERATION, send_result=(False, "no_worker"))
    await _handle_hijack_step(hub, ws, WID)
    assert ws.sent == [_error(NO_WORKER)]
    assert hub.metrics == []
    assert hub.events == []
