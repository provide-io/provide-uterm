#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``browser_handlers._rollback_reclaimed_hijack``.

Kill-suite only — behavioural coverage of the browser resume/hijack flow lives
in ``test_browser_handlers_resume.py`` and friends. Those tests, plus the
``_try_reclaim_hijack``/``_handle_resume`` kill-suites in this directory, only
reach ``_rollback_reclaimed_hijack`` by monkeypatching it out with a stub
(``monkeypatch.setattr(browser_handlers, "_rollback_reclaimed_hijack",
_fake_rollback)``) so the real function is never called from any existing
test. That leaves all 29 of its mutants in the ``no tests`` state, which a
mutation gate can never excuse. Every test here calls the real
``_rollback_reclaimed_hijack`` directly against a strict fake hub that raises
(via argument assertions, or via missing/extra-argument ``TypeError``s from
Python's own call binding) on any unexpected argument.

No documented equivalents: all 29 mutants are killed below.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from provide.uterm.server.bridge.routes import browser_handlers
from provide.uterm.server.bridge.routes.browser_handlers import _rollback_reclaimed_hijack

WID = "browser-worker"
NOW = 5000.0
_WS = object()


class _Hub:
    """A hub whose collaborator methods raise on any unexpected argument.

    ``AsyncMock(return_value=...)`` would answer no matter what it is called
    with, which is exactly why the argument-substitution and dropped-argument
    mutants below are otherwise invisible: they either pass ``None``/a wrong
    value where a real one belongs, or drop an argument outright (which
    Python's own call binding turns into a ``TypeError`` against a fake with
    the real, non-defaulted signature).
    """

    def __init__(self, ws: Any, *, released: bool, rest_active: bool = False) -> None:
        self.ws = ws
        self._released = released
        self._rest_active = rest_active
        self.release_calls: list[tuple[str, Any]] = []
        self.unowned_sends: list[dict[str, Any]] = []
        self.broadcasts: list[str] = []

    async def try_release_ws_hijack(self, worker_id: str, ws: Any) -> tuple[bool, bool]:
        assert worker_id == WID, f"try_release_ws_hijack got worker_id={worker_id!r}"
        assert ws is self.ws, f"try_release_ws_hijack got ws={ws!r}"
        self.release_calls.append((worker_id, ws))
        return self._released, self._rest_active

    async def send_worker_if_unowned(self, worker_id: str, msg: dict[str, Any]) -> bool:
        assert worker_id == WID, f"send_worker_if_unowned got worker_id={worker_id!r}"
        self.unowned_sends.append(msg)
        return True

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        assert worker_id == WID, f"broadcast_hijack_state got worker_id={worker_id!r}"
        self.broadcasts.append(worker_id)


# ---------------------------------------------------------------------------
# A failed release returns immediately.
# ---------------------------------------------------------------------------


async def test_a_failed_release_returns_immediately_without_touching_anything() -> None:
    """Kills mutmut_1 (``released, rest_active = None`` — crashes on unpack
    before ``released`` even exists), mutmut_2/3 (``worker_id``/``ws``
    replaced with ``None`` in the ``try_release_ws_hijack`` call — caught by
    the fake's argument asserts), mutmut_4/5 (``worker_id`` or ``ws`` dropped
    from that call — Python's own binding raises ``TypeError`` against the
    fake's two-parameter signature before the fake body ever runs), and
    mutmut_6 (``if not released:`` -> ``if released:``): with ``released =
    False`` the real code returns immediately and touches nothing further;
    the mutant's flipped guard is False here, so it falls through and calls
    the downstream collaborators the fake would reject as untouched.
    """
    hub = _Hub(_WS, released=False)

    result = await _rollback_reclaimed_hijack(hub, _WS, WID)  # type: ignore[arg-type]

    assert result is None
    assert hub.release_calls == [(WID, _WS)]
    assert hub.unowned_sends == []
    assert hub.broadcasts == []


# ---------------------------------------------------------------------------
# rest_active gates the compensating resume, not the broadcast.
# ---------------------------------------------------------------------------


async def test_rest_active_true_skips_the_compensating_resume_but_still_broadcasts() -> None:
    """Kills mutmut_7 (``if not rest_active:`` -> ``if rest_active:``) in its
    "true" direction: with ``rest_active = True`` the real code must skip the
    compensating resume send, but the mutant's flipped guard is True here and
    would fire it. The broadcast is unconditional (outside the ``rest_active``
    check entirely) and must still happen.
    """
    hub = _Hub(_WS, released=True, rest_active=True)

    await _rollback_reclaimed_hijack(hub, _WS, WID)  # type: ignore[arg-type]

    assert hub.unowned_sends == []
    assert hub.broadcasts == [WID]


async def test_a_successful_rollback_sends_the_exact_compensating_resume_frame_and_broadcasts(
    monkeypatch: Any,
) -> None:
    """Kills mutmut_7's other direction (``rest_active = False`` must send
    the resume; the flipped guard would skip it), mutmut_8/9 (``worker_id``
    or the payload dict replaced with ``None`` in the
    ``send_worker_if_unowned`` call), mutmut_10/11 (``worker_id`` or the
    payload dict dropped from that call entirely — ``TypeError`` from
    Python's call binding against the fake's two-parameter signature),
    mutmut_12-28 (all seventeen key/value literal mutants in the resume
    payload: every key's case, every value's case or content, and
    ``lease_s: 0`` -> ``1`` — any one of them makes the recorded dict compare
    unequal to the exact expected payload), and mutmut_29 (``worker_id`` ->
    ``None`` in the final ``broadcast_hijack_state`` call, caught by the
    fake's argument assert).
    """
    monkeypatch.setattr(browser_handlers, "time", SimpleNamespace(time=lambda: NOW))
    hub = _Hub(_WS, released=True, rest_active=False)

    await _rollback_reclaimed_hijack(hub, _WS, WID)  # type: ignore[arg-type]

    assert hub.unowned_sends == [
        {"type": "control", "action": "resume", "owner": "resume-rollback", "lease_s": 0, "ts": NOW}
    ]
    assert hub.broadcasts == [WID]
