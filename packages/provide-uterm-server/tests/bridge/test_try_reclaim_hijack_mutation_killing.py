#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``browser_handlers._try_reclaim_hijack``.

Kill-suite only — behavioural coverage of the browser resume/hijack flow lives
in ``test_browser_handlers_resume.py`` and friends. Those tests exercise
``_try_reclaim_hijack`` only indirectly through ``_handle_resume``, so a fake
hub answering regardless of arguments (``AsyncMock(return_value=...)``) leaves
every argument-substitution and exact-payload mutant undetected. Every test
here drives the function directly and says which surviving mutant it kills.

``browser_handlers.py`` measured 2026-09-04: 1,119 mutants, 668 survived. This
file closes ``_try_reclaim_hijack``'s 47; the other nine handlers in the module
(``handle_input`` alone had 314) are untouched and tracked separately.

Re-measured under mutmut 3.8 on 2026-09-23 with this suite in the selection:
1,127 mutants, 432 killed, 636 survived, 10 timeouts (score 38.33).
``_try_reclaim_hijack`` has no survivors. ``browser_handlers.py`` is NOT on
the ``[tool.mutmut]`` perimeter yet, and must not be added until every function
in it is closed: listing it today would fail the full-perimeter run. When it
is added, wire this file into ``BRIDGE_HUB_MUTATION_TESTS`` in
``scripts/mutation_gate_config.py`` alongside the ``test_browser_handlers_*``
suites, or scoped runs will not select it.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm.server.bridge.routes import browser_handlers
from provide.uterm.server.bridge.routes.browser_handlers import _try_reclaim_hijack

WID = "browser-worker"


class _ArgCheckingHub:
    """A hub whose collaborator methods raise on any unexpected argument.

    ``AsyncMock(return_value=...)`` answers no matter what it is called with,
    which is why argument-substitution mutants (an id replaced by None, an
    argument dropped, ``worker_id``/``ws`` swapped) survived every existing
    behavioural test.
    """

    def __init__(
        self,
        *,
        reclaimed: bool = True,
        competing_owner: Any = "SENTINEL_COMPETITOR",
        generation: int = 7,
        pause_sent: bool = True,
        pause_reason: str | None = None,
    ) -> None:
        self._reclaimed = reclaimed
        self._competing_owner = competing_owner
        self._generation = generation
        self._pause_sent = pause_sent
        self._pause_reason = pause_reason
        self.reclaim_status_called = False
        self.capture_ownership_called = False
        self.send_owned_worker_calls: list[tuple[str, dict[str, Any], Any, Any]] = []

    async def try_reclaim_hijack_status(self, worker_id: str, ws: Any) -> tuple[bool, Any]:
        assert worker_id == WID, f"try_reclaim_hijack_status got worker_id={worker_id!r}"
        assert ws is _WS, f"try_reclaim_hijack_status got ws={ws!r}"
        self.reclaim_status_called = True
        return self._reclaimed, self._competing_owner

    async def capture_browser_ownership(self, worker_id: str, ws: Any) -> int:
        assert worker_id == WID, f"capture_browser_ownership got worker_id={worker_id!r}"
        assert ws is _WS, f"capture_browser_ownership got ws={ws!r}"
        self.capture_ownership_called = True
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
        assert browser_ws is _WS, f"send_owned_worker got browser_ws={browser_ws!r}"
        assert ownership_generation == self._generation, (
            f"send_owned_worker got ownership_generation={ownership_generation!r}, expected {self._generation!r}"
        )
        self.send_owned_worker_calls.append((worker_id, msg, browser_ws, ownership_generation))
        return self._pause_sent, self._pause_reason


_WS = object()


def _session(*, was_hijack_owner: bool) -> Any:
    return SimpleNamespace(was_hijack_owner=was_hijack_owner)


# ---------------------------------------------------------------------------
# The `session.was_hijack_owner and can_hijack` guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("was_hijack_owner", "can_hijack"),
    [(False, False), (True, False), (False, True)],
)
async def test_the_guard_requires_both_owner_and_can_hijack(was_hijack_owner: bool, can_hijack: bool) -> None:
    """Kills mutmut_2 (``and`` -> ``or``) and the guard's return-value flips
    (mutmut_3/4/5).

    Under the real ``and``, any one of these three combinations must
    short-circuit to exactly ``(False, False, False)`` without touching the
    hub. Under the ``or`` mutant, ``(False, True)`` would fall through instead
    — asserting the hub is never called is what catches it; the ``(True,
    False)`` and ``(False, False)`` cases are what pin the exact tuple.
    """
    hub = _ArgCheckingHub()
    result = await _try_reclaim_hijack(hub, _WS, WID, _session(was_hijack_owner=was_hijack_owner), can_hijack)
    assert result == (False, False, False)
    assert hub.reclaim_status_called is False


# ---------------------------------------------------------------------------
# hub.try_reclaim_hijack_status — exact args, and the failure return
# ---------------------------------------------------------------------------


async def test_reclaim_status_is_queried_with_the_exact_worker_and_socket() -> None:
    """Kills mutmut_8 (``ws`` -> ``None``): the fake raises on any mismatch,
    so this only passes if the real arguments reach the call."""
    hub = _ArgCheckingHub(reclaimed=True)
    await _try_reclaim_hijack(hub, _WS, WID, _session(was_hijack_owner=True), True)
    assert hub.reclaim_status_called is True


async def test_a_failed_reclaim_returns_the_competing_owner_untouched() -> None:
    """Pins the exact tuple on the ``not reclaimed_hijack`` branch and proves
    ``competing_owner`` passes through unmodified — a distinctive sentinel,
    not a bool, so a mutant that drops or coerces it cannot hide as a lucky
    False."""
    hub = _ArgCheckingHub(reclaimed=False, competing_owner="rival-dashboard")
    result = await _try_reclaim_hijack(hub, _WS, WID, _session(was_hijack_owner=True), True)
    assert result == (False, False, "rival-dashboard")
    assert hub.capture_ownership_called is False


# ---------------------------------------------------------------------------
# hub.capture_browser_ownership — exact args
# ---------------------------------------------------------------------------


async def test_ownership_is_captured_with_the_exact_worker_and_socket() -> None:
    """Kills mutmut_14 (call dropped, generation forced None), 15
    (``worker_id`` -> None), 16 (``ws`` -> None), 17 (``ws`` passed as the
    only/first arg), 18 (``ws`` dropped, trailing comma). Each either raises
    against the arg-checking fake or produces a generation that fails the
    downstream ``send_owned_worker`` check in ``_ArgCheckingHub``."""
    hub = _ArgCheckingHub(generation=99, pause_sent=True)
    await _try_reclaim_hijack(hub, _WS, WID, _session(was_hijack_owner=True), True)
    assert hub.capture_ownership_called is True
    assert hub.send_owned_worker_calls[0][3] == 99


# ---------------------------------------------------------------------------
# hub.send_owned_worker — exact pause-frame payload
# ---------------------------------------------------------------------------


async def test_the_pause_frame_is_exactly_the_documented_control_message() -> None:
    """Kills mutmut_21 (dict -> None) and the sixteen key/value literal
    mutants (mutmut_28-44: every key and every non-timestamp value mangled in
    case or content). ``ts`` is asserted only for type/recency — it is
    ``time.time()`` at call time, not a fixed literal.
    """
    before = time.time()
    hub = _ArgCheckingHub()
    await _try_reclaim_hijack(hub, _WS, WID, _session(was_hijack_owner=True), True)
    after = time.time()

    _worker_id, msg, _browser_ws, _generation = hub.send_owned_worker_calls[0]
    ts = msg.pop("ts")
    assert msg == {"type": "control", "action": "pause", "owner": "dashboard", "lease_s": 0}
    assert before <= ts <= after


# ---------------------------------------------------------------------------
# pause not sent -> rollback, with exact args, and the returned tuple
# ---------------------------------------------------------------------------


async def test_pause_not_sent_rolls_back_with_the_exact_hub_socket_and_worker(monkeypatch: Any) -> None:
    """Kills mutmut_46-51 (every argument to ``_rollback_reclaimed_hijack``
    dropped or replaced with None) and the rollback branch's return-value
    flips (mutmut_52/53/54)."""
    calls: list[tuple[Any, Any, Any]] = []

    async def _fake_rollback(hub: Any, ws: Any, worker_id: str) -> None:
        calls.append((hub, ws, worker_id))

    monkeypatch.setattr(browser_handlers, "_rollback_reclaimed_hijack", _fake_rollback)
    hub = _ArgCheckingHub(pause_sent=False)

    result = await _try_reclaim_hijack(hub, _WS, WID, _session(was_hijack_owner=True), True)

    assert result == (False, False, False)
    assert calls == [(hub, _WS, WID)]


# ---------------------------------------------------------------------------
# pause sent -> success return
# ---------------------------------------------------------------------------


async def test_pause_sent_returns_owned_and_reclaimed_without_a_competing_owner() -> None:
    """Kills mutmut_55/56/57, the success tuple's three field flips."""
    hub = _ArgCheckingHub(pause_sent=True)
    result = await _try_reclaim_hijack(hub, _WS, WID, _session(was_hijack_owner=True), True)
    assert result == (True, True, False)
