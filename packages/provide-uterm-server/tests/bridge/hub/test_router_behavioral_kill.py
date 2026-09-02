#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for ``router_behavioral`` — exact values, not "not negative".

The existing coverage asserts ``h["cps"] >= 0.0`` and ``h["jitter"] >= 0.0``.
Both hold for every arithmetic mutant of the two expressions that produce them:
divide becomes multiply, ``range(1, n)`` becomes ``range(2, n)``, a guard flips
from ``> 1`` to ``>= 1``, and the tautology still passes. That is how nine
mutants sat alive under 100% line coverage. The tests here assert the number.

The keystroke ring's ``maxlen=50`` has the same shape: nothing counted, so the
bound could be raised or removed entirely — and removing it turns a per-browser
ring buffer into an unbounded list that grows for as long as the browser is
connected, which is the reason the bound exists.

The audit paths are covered here rather than by
``test_easy_coverage_gaps_part{2,4}.py``, which cannot be wired into a mutation
run: part2 imports ``tests.helpers``, an absolute import that resolves only
under the server package's own rootdir, and mutmut's baseline run is ``-x`` --
so that one unrelated ``ModuleNotFoundError`` aborted all 112 mutants before a
single one was checked.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import deque
from typing import Any
from unittest.mock import AsyncMock

import pytest

from provide.uterm.server.bridge.hub import PolicyContext, PolicyDecision, TermHub
from provide.uterm.server.bridge.hub.ext import BehavioralThresholds, ConnectionHeuristics

_RING_MAX = 50


@pytest.fixture()
def hub() -> TermHub:
    return TermHub()


def _seed(hub: TermHub, source: object, stamps: list[float]) -> None:
    """Install a known timestamp series, so the metrics are arithmetic, not timing."""
    hub.router.keystroke_timestamps[source] = deque(stamps, maxlen=_RING_MAX)


def _logged_verbatim(caplog: pytest.LogCaptureFixture, phrase: str) -> bool:
    """True if *phrase* appears in a log line as itself, not as part of a longer word.

    A plain ``in`` check cannot kill a string-literal mutation: mutmut rewrites
    ``"foo"`` as ``"XXfooXX"``, and that still *contains* ``foo``. Equality is
    not available either -- telemetry renders each record with a timestamp,
    level and logger name around the message. Word-boundary lookarounds are the
    discriminator that is left, and they are exactly the right one: the sentinel
    wrapping glues word characters onto both ends of the literal.
    """
    pattern = rf"(?<!\w){re.escape(phrase)}(?!\w)"
    return any(re.search(pattern, record.getMessage()) for record in caplog.records)


# ---------------------------------------------------------------------------
# record_keystroke — the ring bound
# ---------------------------------------------------------------------------


def test_the_keystroke_ring_stops_at_fifty_and_drops_the_oldest(hub: TermHub) -> None:
    """``maxlen=50`` is load-bearing: without it this grows for the whole session.

    Pins both the bound and that it is a *ring* — an unbounded deque (maxlen
    None) and an off-by-one bound both fail here.
    """
    source = object()
    for _ in range(_RING_MAX + 10):
        hub.router.record_keystroke(source)

    ring = hub.router.keystroke_timestamps[source]

    assert len(ring) == _RING_MAX
    assert ring.maxlen == _RING_MAX


# ---------------------------------------------------------------------------
# get_heuristics — the arithmetic
# ---------------------------------------------------------------------------


def test_two_keystrokes_are_enough_to_report_metrics(hub: TermHub) -> None:
    """The guard is ``< 2``: two stamps is one interval, which is measurable.

    ``cps`` is (n-1)/duration = 1/2 = 0.5 -- asserted exactly, so multiplying
    instead of dividing (which gives 2.0 here) cannot pass. Requiring three
    stamps instead would return zeros.
    """
    source = object()
    _seed(hub, source, [0.0, 2.0])

    assert hub.router.get_heuristics(source) == {"cps": 0.5, "jitter": 0.0}


def test_a_single_interval_reports_no_jitter_rather_than_raising(hub: TermHub) -> None:
    """``variance`` of one sample raises, so the ``> 1`` guard is not cosmetic.

    Relaxing it to ``>= 1`` turns this into a StatisticsError on a live audit
    path; the ``else`` branch must also stay 0.0, not some other constant.
    """
    source = object()
    _seed(hub, source, [10.0, 11.0])

    assert hub.router.get_heuristics(source)["jitter"] == 0.0


def test_identical_timestamps_give_zero_cps_instead_of_dividing_by_zero(hub: TermHub) -> None:
    """Two keystrokes in the same clock tick make ``duration`` exactly 0.

    The guard is ``duration > 0``; at ``>= 0`` this divides by zero. The zero
    branch must yield 0.0 -- a mutant returning 1.0 would report one character
    per second for a burst that took no time at all.
    """
    source = object()
    _seed(hub, source, [5.0, 5.0])

    assert hub.router.get_heuristics(source)["cps"] == 0.0


def test_jitter_is_the_variance_of_every_gap_including_the_first(hub: TermHub) -> None:
    """Intervals run from index 1, so gaps are [1.0, 3.0] and the variance is 2.0.

    Starting the range at 2 silently drops the first gap, leaving a single
    sample and a jitter of 0.0. Requiring ``> 2`` intervals does the same. Both
    read as "the typist is perfectly regular", which is the opposite of true.
    """
    source = object()
    _seed(hub, source, [0.0, 1.0, 4.0])

    result = hub.router.get_heuristics(source)

    assert result["jitter"] == 2.0
    assert result["cps"] == 0.5


# ---------------------------------------------------------------------------
# forget_browser
# ---------------------------------------------------------------------------


def test_forgetting_a_browser_drops_its_ring(hub: TermHub) -> None:
    source = object()
    hub.router.record_keystroke(source)

    hub.router.forget_browser(source)

    assert source not in hub.router.keystroke_timestamps


def test_forgetting_an_unknown_browser_is_a_no_op(hub: TermHub) -> None:
    """Disconnects can arrive for a browser that never typed."""
    hub.router.forget_browser(object())

    assert hub.router.keystroke_timestamps == {}


# ---------------------------------------------------------------------------
# audit_all_browsers — the deny path
# ---------------------------------------------------------------------------


class _RecordingGate:
    """Gate that records what it was asked and answers with a fixed decision."""

    def __init__(self, decision: PolicyDecision) -> None:
        self.decision = decision
        self.seen: list[tuple[ConnectionHeuristics, PolicyContext]] = []
        self.thresholds: BehavioralThresholds | None = None

    async def audit_connection(
        self,
        heuristics: ConnectionHeuristics,
        context: PolicyContext,
        thresholds: BehavioralThresholds,
    ) -> PolicyDecision:
        self.seen.append((heuristics, context))
        self.thresholds = thresholds
        return self.decision


async def _hub_with_gate(gate: Any) -> tuple[TermHub, AsyncMock]:
    hub = TermHub(behavioral_audit_gate=gate)
    await hub.register_worker("w1", AsyncMock())
    browser = AsyncMock()
    await hub.register_browser("w1", browser, "admin")
    return hub, browser


async def test_a_denied_browser_is_closed_with_the_policy_violation_code() -> None:
    """1008 is the policy-violation close code; the reason is the gate's own."""
    gate = _RecordingGate(PolicyDecision(action="deny", reason="too noisy"))
    hub, browser = await _hub_with_gate(gate)

    await hub.router.audit_all_browsers()

    browser.close.assert_awaited_once()
    kwargs = browser.close.await_args.kwargs
    assert kwargs["code"] == 1008
    assert kwargs["reason"] == "too noisy"


async def test_a_denial_without_a_reason_still_closes_with_a_stated_one() -> None:
    """The fallback text is what the browser is told; an empty reason is not useful."""
    gate = _RecordingGate(PolicyDecision(action="deny", reason=None))
    hub, browser = await _hub_with_gate(gate)

    await hub.router.audit_all_browsers()

    assert browser.close.await_args.kwargs["reason"] == "Behavioral anomaly"


async def test_an_allowed_browser_is_left_open() -> None:
    """Only ``deny`` closes. Any other action -- including an unknown one -- does not."""
    gate = _RecordingGate(PolicyDecision(action="allow", reason=None))
    hub, browser = await _hub_with_gate(gate)

    await hub.router.audit_all_browsers()

    browser.close.assert_not_awaited()


async def test_a_close_that_fails_does_not_abort_the_remaining_audit() -> None:
    """The suppress is deliberate: one wedged socket must not skip every browser after it."""
    gate = _RecordingGate(PolicyDecision(action="deny", reason="nope"))
    hub, browser = await _hub_with_gate(gate)
    browser.close.side_effect = RuntimeError("socket already gone")

    await hub.router.audit_all_browsers()

    browser.close.assert_awaited_once()


async def test_the_gate_is_asked_under_the_behavioral_audit_action() -> None:
    """The action names the decision the policy is being asked to make.

    It reaches the gate through the policy context, so a different string
    silently asks a different question.
    """
    gate = _RecordingGate(PolicyDecision(action="allow", reason=None))
    hub, _browser = await _hub_with_gate(gate)

    await hub.router.audit_all_browsers()

    assert len(gate.seen) == 1
    _heuristics, context = gate.seen[0]
    assert context.action == "behavioral_audit"


async def test_the_gate_receives_the_browser_own_measured_metrics() -> None:
    """A gate handed zeros for a fast typist cannot make the decision it exists for."""
    gate = _RecordingGate(PolicyDecision(action="allow", reason=None))
    hub, browser = await _hub_with_gate(gate)
    hub.router.keystroke_timestamps[browser] = deque([0.0, 1.0, 4.0], maxlen=_RING_MAX)

    await hub.router.audit_all_browsers()

    heuristics, _context = gate.seen[0]
    assert heuristics.cps == 0.5
    assert heuristics.jitter == 2.0


# ---------------------------------------------------------------------------
# run_behavioral_audit_loop
# ---------------------------------------------------------------------------


async def test_the_audit_loop_keeps_running_after_an_audit_raises() -> None:
    """A gate that throws must not silently end auditing for the process lifetime.

    The loop sleeps on the configured interval and swallows the error, so this
    waits for a *second* call: one call only proves it started.
    """
    hub = TermHub()
    hub._behavioral_audit_interval_s = 0.001  # type: ignore[attr-defined]
    calls = 0

    async def _boom() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("gate exploded")

    hub._audit_all_browsers = _boom  # type: ignore[assignment]
    task = asyncio.create_task(hub.router.run_behavioral_audit_loop())
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while calls < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.001)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert calls >= 2, "the loop stopped after the first failing audit"


async def test_the_policy_context_names_the_browser_and_the_worker_it_belongs_to() -> None:
    """Both arguments carry identity, and dropping either one asks about nobody.

    ``worker_id`` is what the decision is recorded against; the browser is what
    the role is resolved from. A gate handed a context with either missing
    cannot scope its answer.
    """
    gate = _RecordingGate(PolicyDecision(action="allow", reason=None))
    hub, _browser = await _hub_with_gate(gate)

    await hub.router.audit_all_browsers()

    _heuristics, context = gate.seen[0]
    assert context.worker_id == "w1"
    assert context.role == "admin"


async def test_the_gate_is_given_the_hub_configured_thresholds() -> None:
    """Thresholds are the yardstick; without them the gate has metrics and no bar."""
    gate = _RecordingGate(PolicyDecision(action="allow", reason=None))
    hub = TermHub(behavioral_audit_gate=gate)
    await hub.register_worker("w1", AsyncMock())
    await hub.register_browser("w1", AsyncMock(), "admin")

    await hub.router.audit_all_browsers()

    assert gate.thresholds is hub._behavioral_thresholds
    assert gate.thresholds is not None


async def test_the_denial_is_logged_against_the_worker_with_its_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A close with no audit trail leaves nobody able to say why a session ended."""
    gate = _RecordingGate(PolicyDecision(action="deny", reason="too noisy"))
    hub, _browser = await _hub_with_gate(gate)
    caplog.set_level(logging.WARNING)

    await hub.router.audit_all_browsers()

    assert _logged_verbatim(caplog, "behavioral_audit_denied worker_id=w1 reason=too noisy")


async def test_a_denial_with_no_reason_is_logged_with_the_fallback_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``or`` supplies the fallback; ``and`` would log the word None instead.

    The exact text is asserted because a case-changed or sentinel-wrapped
    variant is still "a string in the log" and still tells the reader nothing.
    """
    gate = _RecordingGate(PolicyDecision(action="deny", reason=None))
    hub, _browser = await _hub_with_gate(gate)
    caplog.set_level(logging.WARNING)

    await hub.router.audit_all_browsers()

    assert _logged_verbatim(caplog, "behavioral_audit_denied worker_id=w1 reason=anomaly detected")


async def test_the_swallowed_audit_error_is_logged_under_its_own_event_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Swallowing the exception is only safe if it is still recorded somewhere."""
    hub = TermHub()
    hub._behavioral_audit_interval_s = 0.001  # type: ignore[attr-defined]

    async def _boom() -> None:
        raise RuntimeError("gate exploded")

    hub._audit_all_browsers = _boom  # type: ignore[assignment]
    caplog.set_level(logging.ERROR)
    task = asyncio.create_task(hub.router.run_behavioral_audit_loop())
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while not _logged_verbatim(caplog, "behavioral_audit_loop_error"):
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(0.001)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert _logged_verbatim(caplog, "behavioral_audit_loop_error")
