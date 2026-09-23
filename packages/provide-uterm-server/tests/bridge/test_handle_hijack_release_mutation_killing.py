#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``browser_handlers._handle_hijack_release``.

Kill-suite only — behavioural coverage of the WebSocket hijack flow lives in
``test_browser_handlers_*.py``. Those suites check the outcome of a release
but not the exact re-check argument, the compensating ``resume`` payload, the
metric name, or the event; their hubs answer regardless of arguments. Under
mutmut 3.8 (2026-09-23) ``_handle_hijack_release`` had 18 survivors, every one
of them on the ``_do_resume`` re-check, the ``broadcast``/``notify`` argument,
the release metric, or the exact ``hijack_released`` event.

Everything here drives ``_handle_hijack_release`` directly against a strict
hub that raises on any unexpected argument, with the clock pinned so the
resume payload's ``ts`` is exact.

Documented equivalents:

- mutmut_10 (``_do_resume = False`` -> ``_do_resume = None`` in the re-check
  branch) is genuinely equivalent. ``_do_resume`` is only ever read in
  boolean context afterwards (``if _do_resume:`` twice) and the function's
  return value is a hard-coded ``False``/``owned_hijack`` literal, never
  ``_do_resume`` itself, so ``False`` and ``None`` are indistinguishable at
  every observation point. mutmut_11, which flips the same assignment to
  ``True`` instead, *is* observable (it leaves the compensating resume and
  the notify armed) and is killed below by
  ``test_a_recheck_that_finds_the_hijack_taken_again_skips_resume_and_notify``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm.server.bridge.routes import browser_handlers
from provide.uterm.server.bridge.routes.browser_handlers import _handle_hijack_release

WID = "hijack-worker"
NOW = 1000.0
RESUME = {"type": "control", "action": "resume", "owner": "dashboard", "lease_s": 0, "ts": NOW}
RELEASED_EVENT = (WID, "hijack_released", {"owner": "dashboard_ws"})


class _WS:
    """Identity marker for the calling browser connection."""


class _Hub:
    """A hub whose collaborators raise on any unexpected argument."""

    def __init__(
        self,
        ws: _WS,
        *,
        released: bool,
        rest_active: bool = False,
        still_hijacked: bool = False,
    ) -> None:
        self.ws = ws
        self._released = released
        self._rest_active = rest_active
        self._still_hijacked = still_hijacked
        self.still_hijacked_checks: list[str] = []
        self.unowned_sends: list[dict[str, Any]] = []
        self.broadcasts: list[str] = []
        self.notifications: list[tuple[str, bool, str | None]] = []
        self.metrics: list[str] = []
        self.events: list[tuple[str, str, dict[str, Any] | None]] = []

    async def try_release_ws_hijack(self, worker_id: str, ws: Any) -> tuple[bool, bool]:
        assert (worker_id, ws) == (WID, self.ws), f"try_release_ws_hijack got {(worker_id, ws)!r}"
        return self._released, self._rest_active

    async def check_still_hijacked(self, worker_id: str) -> bool:
        assert worker_id == WID, f"check_still_hijacked got {worker_id!r}"
        self.still_hijacked_checks.append(worker_id)
        return self._still_hijacked

    async def send_worker_if_unowned(self, worker_id: str, msg: dict[str, Any]) -> bool:
        assert worker_id == WID, f"send_worker_if_unowned got {worker_id!r}"
        assert isinstance(msg, dict), f"send_worker_if_unowned got {msg!r}"
        self.unowned_sends.append(msg)
        return True

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        self.broadcasts.append(worker_id)

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None) -> None:
        self.notifications.append((worker_id, enabled, owner))

    def metric(self, name: str) -> None:
        self.metrics.append(name)

    async def append_event(self, worker_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
        self.events.append((worker_id, event, payload))


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_handlers, "time", SimpleNamespace(time=lambda: NOW))


@pytest.mark.parametrize("owned", [True, False])
async def test_a_release_that_did_not_own_the_hijack_leaves_ownership_and_touches_nothing(owned: bool) -> None:
    """A failed release returns the caller's ``owned_hijack`` unchanged, in
    either direction, and calls none of the downstream hub collaborators."""
    ws = _WS()
    hub = _Hub(ws, released=False)
    assert await _handle_hijack_release(hub, ws, WID, owned_hijack=owned) is owned
    assert hub.still_hijacked_checks == []
    assert hub.unowned_sends == []
    assert hub.broadcasts == []
    assert hub.notifications == []
    assert hub.metrics == []
    assert hub.events == []


async def test_a_release_with_an_active_rest_lease_skips_the_recheck_and_the_resume() -> None:
    """rest_active=True means ``_do_resume`` starts (and stays) False, so the
    ``and`` short-circuits and ``check_still_hijacked`` is never even called —
    the resume and notify are skipped. The tail (broadcast/metric/event)
    still runs unconditionally, which kills mutmut_33 (broadcast argument),
    mutmut_40/41/42 (metric name), and mutmut_43/44/45/48/49/50/51/52/53/54
    (every field of the ``append_event`` call, including the dropped
    argument in 48)."""
    ws = _WS()
    hub = _Hub(ws, released=True, rest_active=True)
    assert await _handle_hijack_release(hub, ws, WID, owned_hijack=True) is False
    assert hub.still_hijacked_checks == []
    assert hub.unowned_sends == []
    assert hub.notifications == []
    assert hub.broadcasts == [WID]
    assert hub.metrics == ["hijack_releases_total"]
    assert hub.events == [RELEASED_EVENT]


async def test_a_recheck_that_finds_the_hijack_taken_again_skips_resume_and_notify() -> None:
    """rest_active=False starts ``_do_resume`` True, so
    ``check_still_hijacked`` is called with the real worker id (kills
    mutmut_9, which passes ``None`` instead). It answers True (someone else
    grabbed the hijack), which must cancel the resume: no compensating
    ``resume`` frame and no ``notify_hijack_changed`` call. This kills
    mutmut_11, which reassigns ``_do_resume = True`` instead of ``False`` in
    that branch — under the mutant, both calls would fire."""
    ws = _WS()
    hub = _Hub(ws, released=True, rest_active=False, still_hijacked=True)
    assert await _handle_hijack_release(hub, ws, WID, owned_hijack=True) is False
    assert hub.still_hijacked_checks == [WID]
    assert hub.unowned_sends == []
    assert hub.notifications == []
    assert hub.broadcasts == [WID]
    assert hub.metrics == ["hijack_releases_total"]
    assert hub.events == [RELEASED_EVENT]


async def test_a_clean_release_resumes_the_worker_and_notifies_listeners() -> None:
    """rest_active=False and no concurrent re-acquire (still_hijacked=False)
    leaves ``_do_resume`` True for both guarded calls: an exact compensating
    ``resume`` frame is sent and ``notify_hijack_changed`` fires with the
    worker id (kills mutmut_34, which passes ``None`` there). The tail still
    runs unconditionally."""
    ws = _WS()
    hub = _Hub(ws, released=True, rest_active=False, still_hijacked=False)
    assert await _handle_hijack_release(hub, ws, WID, owned_hijack=True) is False
    assert hub.still_hijacked_checks == [WID]
    assert hub.unowned_sends == [RESUME]
    assert hub.notifications == [(WID, False, None)]
    assert hub.broadcasts == [WID]
    assert hub.metrics == ["hijack_releases_total"]
    assert hub.events == [RELEASED_EVENT]
