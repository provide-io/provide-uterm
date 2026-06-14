#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Deterministic mutation-killing suite for :class:`PollingCoordinator`.

``test_hub_polling_coverage.py`` drives the real hub against the wall clock,
so it cannot pin the loop-deadline arithmetic or the ``max(...)`` clamps —
several of its "mutation" assertions are tautologies over Python builtins
(``assert max(20, 10) == 20``) that never execute the mutated source line.

This suite replaces the module's ``time`` and ``asyncio`` with a *fake clock*
scoped to ``polling_service`` only (so the event loop is untouched). The
patched ``sleep`` advances monotonic by an exact **1.0** per call (so loop
*iteration counts* are exact integers, free of float drift) while *recording*
the real ``seconds`` argument (so the interval/sleep-constant *values* are
observable). A deadline mutant (``/1000`` → ``*1000``/``/1001``, ``<`` →
``<=``) therefore changes the iteration count; an interval mutant
(``max(20, …)`` → ``max(21, …)``, ``/1000`` → ``*1000``/``/1001``) changes the
recorded sleep value; a ``.get`` default/key or re-request-gate mutant changes
the re-request count; and a runaway (``*1000`` deadline) trips the iteration
cap and raises — all caught by the assertions below.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm.server.bridge.hub import polling_service
from provide.uterm.server.bridge.hub.polling_service import PollingCoordinator

_ITERATION_CAP = 5000  # legit tests use <=1002 iterations; *1000 deadline mutants blow past this


class _IterationCapError(RuntimeError):
    """Raised when a (mutated) loop exceeds the deterministic iteration cap."""


class _FakeClock:
    """Monotonic + wall clock; ``sleep`` advances monotonic by exactly 1.0."""

    def __init__(self, *, wall: float = 5000.0) -> None:
        self.mono = 0.0
        self.wall = wall
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)  # record the real arg (interval / 0.08)
        self.mono += 1.0  # advance by an exact integer → drift-free iteration counts
        if len(self.sleeps) > _ITERATION_CAP:
            raise _IterationCapError


class _FakeRegistry:
    def __init__(self, states: dict[str, Any]) -> None:
        self._workers = states

    def get(self, worker_id: str) -> Any:
        return self._workers.get(worker_id)


class _FakeHub:
    def __init__(self, states: dict[str, Any] | None = None) -> None:
        self._lock = asyncio.Lock()
        self.registry = _FakeRegistry(states or {})
        self.requests: list[Any] = []

    async def request_snapshot(self, worker_id: Any) -> None:
        self.requests.append(worker_id)


def _worker(snapshot: dict[str, Any] | None) -> Any:
    return SimpleNamespace(last_snapshot=snapshot)


def _install_clock(monkeypatch: pytest.MonkeyPatch, clock: _FakeClock) -> None:
    """Scope the fake clock + advancing sleep to the polling module only."""
    monkeypatch.setattr(polling_service, "time", SimpleNamespace(monotonic=clock.monotonic, time=clock.time))
    monkeypatch.setattr(polling_service, "asyncio", SimpleNamespace(sleep=clock.sleep))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    _install_clock(monkeypatch, fake)
    return fake


# === snapshot_matches / _compile_guard_regex (static) =======================


def test_snapshot_matches_none_is_false() -> None:
    assert PollingCoordinator.snapshot_matches(None, expect_prompt_id=None, expect_regex=None) is False


def test_compile_guard_regex_empty_is_none_none() -> None:
    assert PollingCoordinator._compile_guard_regex("") == (None, None)
    assert PollingCoordinator._compile_guard_regex(None) == (None, None)


def test_compile_guard_regex_valid_applies_ignorecase_multiline() -> None:
    pattern, err = PollingCoordinator._compile_guard_regex("^ab+c")
    assert err is None
    assert pattern is not None
    assert pattern.search("XX\nABBBC") is not None  # IGNORECASE + MULTILINE both applied


def test_compile_guard_regex_invalid_returns_message() -> None:
    pattern, err = PollingCoordinator._compile_guard_regex("(unclosed")
    assert pattern is None
    assert err is not None and "invalid expect_regex" in err


# === wait_for_snapshot ======================================================


async def test_wait_for_snapshot_returns_fresh_and_requests_worker(clock: _FakeClock) -> None:
    """Fresh snapshot (ts > req_ts) returns on the first poll; pre-loop request used worker_id."""
    snap = {"screen": "fresh", "ts": clock.wall + 1.0}
    hub = _FakeHub({"w": _worker(snap)})
    out = await PollingCoordinator(hub).wait_for_snapshot("w", timeout_ms=1500)
    assert out is snap
    assert hub.requests == ["w"]  # not [None] — kills the request_snapshot(None) mutant


async def test_wait_for_snapshot_missing_worker_returns_none(clock: _FakeClock) -> None:
    out = await PollingCoordinator(_FakeHub({})).wait_for_snapshot("ghost", timeout_ms=1500)
    assert out is None


async def test_wait_for_snapshot_deadline_arithmetic(clock: _FakeClock) -> None:
    """A never-fresh snapshot polls exactly ``timeout_ms/1000`` times then returns None.

    With the 1.0/iteration clock, deadline 1001.0s ⇒ exactly 1001 polls. Pins
    ``/1000`` (``*1000`` trips the cap; ``/1001`` ⇒ 1000), the ``<`` vs ``<=``
    loop guard (``<=`` ⇒ 1002), and the ``sleep(0.08)`` constant (recorded).
    """
    hub = _FakeHub({"w": _worker({"screen": "stale", "ts": 1.0})})  # ts ≪ wall ⇒ never fresh
    out = await PollingCoordinator(hub).wait_for_snapshot("w", timeout_ms=1_001_000)
    assert out is None
    assert len(clock.sleeps) == 1001
    assert clock.sleeps[0] == 0.08  # kills sleep(0.08) → sleep(1.08)


async def test_wait_for_snapshot_missing_ts_key_defaults_to_zero(clock: _FakeClock) -> None:
    """A snapshot lacking ``ts`` is never fresh — default must be 0, not None.

    ``get("ts", None)`` / ``get("ts")`` ⇒ ``None > req_ts`` → TypeError;
    ``get("ts", 0)`` keeps polling and returns None cleanly.
    """
    hub = _FakeHub({"w": _worker({"screen": "no-ts"})})
    out = await PollingCoordinator(hub).wait_for_snapshot("w", timeout_ms=2000)
    assert out is None  # deadline 2.0s ⇒ 2 polls, no TypeError
    assert len(clock.sleeps) == 2


async def test_wait_for_snapshot_missing_ts_default_below_req_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    """With req_ts in (0, 1), a missing-ts default of 1 would falsely look fresh.

    Pins ``get("ts", 0)`` vs ``get("ts", 1)``: at wall-clock 0.5 the default 0
    is not > 0.5 (keep polling → None); a default of 1 would be (returns snap).
    """
    clock = _FakeClock(wall=0.5)
    _install_clock(monkeypatch, clock)
    hub = _FakeHub({"w": _worker({"screen": "no-ts"})})
    out = await PollingCoordinator(hub).wait_for_snapshot("w", timeout_ms=2000)
    assert out is None


async def test_wait_for_snapshot_ts_equal_req_is_not_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """ts == req_ts must NOT count as fresh (pins ``>`` vs ``>=``)."""
    clock = _FakeClock(wall=1234.0)
    _install_clock(monkeypatch, clock)
    hub = _FakeHub({"w": _worker({"screen": "boundary", "ts": 1234.0})})
    out = await PollingCoordinator(hub).wait_for_snapshot("w", timeout_ms=2000)
    assert out is None


# === wait_for_guard: regex compile + no-constraint fast path ================


async def test_wait_for_guard_regex_error_short_circuits(clock: _FakeClock) -> None:
    hub = _FakeHub({"w": _worker({"screen": "x"})})
    ok, out, reason = await PollingCoordinator(hub).wait_for_guard(
        "w", expect_prompt_id=None, expect_regex="(unclosed", timeout_ms=1000, poll_interval_ms=20
    )
    assert ok is False
    assert out is None
    assert reason is not None and "invalid expect_regex" in reason
    assert hub.requests == []  # never reached the request


async def test_wait_for_guard_fastpath_returns_existing_snapshot(clock: _FakeClock) -> None:
    """No prompt-id and no regex → (True, last_snapshot, None) + one worker request."""
    snap = {"screen": "ready", "ts": 1.0}
    hub = _FakeHub({"w": _worker(snap)})
    ok, out, reason = await PollingCoordinator(hub).wait_for_guard(
        "w", expect_prompt_id=None, expect_regex=None, timeout_ms=1000, poll_interval_ms=20
    )
    assert ok is True
    assert out is snap  # pins get(worker_id), st-is-not-None ternary, snap passthrough
    assert reason is None
    assert hub.requests == ["w"]  # pins request_snapshot(worker_id), not (None)


async def test_wait_for_guard_fastpath_missing_worker_snapshot_none(clock: _FakeClock) -> None:
    hub = _FakeHub({})
    ok, out, reason = await PollingCoordinator(hub).wait_for_guard(
        "ghost", expect_prompt_id=None, expect_regex=None, timeout_ms=1000, poll_interval_ms=20
    )
    assert ok is True
    assert out is None
    assert reason is None


# === wait_for_guard: polling loop ==========================================


async def test_wait_for_guard_matches_immediately_on_prompt(clock: _FakeClock) -> None:
    hub = _FakeHub({"w": _worker({"prompt_detected": {"prompt_id": "menu"}, "screen": "Menu"})})
    ok, out, reason = await PollingCoordinator(hub).wait_for_guard(
        "w", expect_prompt_id="menu", expect_regex=None, timeout_ms=1000, poll_interval_ms=20
    )
    assert ok is True
    assert reason is None
    assert hub.requests == ["w"]  # matched before any re-request


async def test_wait_for_guard_deadline_and_interval_arithmetic(clock: _FakeClock) -> None:
    """Never-matching guard polls exactly ``max(50, timeout)/1000`` times.

    deadline 1001.0s ⇒ 1001 polls. Pins the deadline ``/1000`` (``*1000`` trips
    the cap; ``/1001`` ⇒ 1000) and the ``<`` vs ``<=`` guard (``<=`` ⇒ 1002).
    The recorded interval value pins ``max(20, …)/1000`` (``max(21, …)`` ⇒
    0.021, ``*1000`` ⇒ 20.0, ``/1001`` ⇒ 0.019980).
    """
    hub = _FakeHub({"w": _worker({"screen": "no-prompt", "ts": 7.0})})
    ok, out, reason = await PollingCoordinator(hub).wait_for_guard(
        "w", expect_prompt_id="never", expect_regex=None, timeout_ms=1_001_000, poll_interval_ms=20
    )
    assert ok is False
    assert reason == "prompt_guard_not_satisfied"
    assert len(clock.sleeps) == 1001
    assert clock.sleeps[0] == pytest.approx(0.02, rel=1e-9)  # interval = max(20, 20)/1000


async def test_wait_for_guard_small_timeout_records_interval_without_runaway(clock: _FakeClock) -> None:
    hub = _FakeHub({"w": _worker({"screen": "no-prompt", "ts": 7.0})})

    ok, out, reason = await PollingCoordinator(hub).wait_for_guard(
        "w", expect_prompt_id="never", expect_regex=None, timeout_ms=50, poll_interval_ms=20
    )

    assert ok is False
    assert out == {"screen": "no-prompt", "ts": 7.0}
    assert reason == "prompt_guard_not_satisfied"
    assert clock.sleeps == [pytest.approx(0.02, rel=1e-9)]


async def test_wait_for_guard_regex_obj_is_passed_to_matcher(clock: _FakeClock) -> None:
    """A non-matching regex keeps the guard polling (pins expect_regex=regex_obj).

    If the matcher were called with ``expect_regex=None`` the no-constraint
    snapshot would match immediately and wrongly return success.
    """
    hub = _FakeHub({"w": _worker({"screen": "totally other"})})
    ok, out, reason = await PollingCoordinator(hub).wait_for_guard(
        "w", expect_prompt_id=None, expect_regex="WONTMATCH", timeout_ms=5000, poll_interval_ms=20
    )
    assert ok is False
    assert reason == "prompt_guard_not_satisfied"


async def test_wait_for_guard_all_requests_target_worker(clock: _FakeClock) -> None:
    """Every snapshot request (pre-loop + re-requests) targets worker_id, not None."""
    hub = _FakeHub({"w": _worker({"screen": "x", "ts": 3.0})})
    await PollingCoordinator(hub).wait_for_guard(
        "w", expect_prompt_id="never", expect_regex=None, timeout_ms=5000, poll_interval_ms=20
    )
    assert hub.requests  # at least the pre-loop request happened
    assert all(r == "w" for r in hub.requests)


async def test_wait_for_guard_rerequest_count_with_static_ts(clock: _FakeClock) -> None:
    """A snapshot whose ts stays in (0, 1) re-requests only from the 2nd poll on.

    Pins ``last_snap_ts = 0.0`` initial and ``snap.get("ts", 0.0)`` key/default:
    on the first poll snap_ts (0.5) > last_snap_ts (0) so NO re-request; a mutant
    starting last_snap_ts at 1.0, or reading the wrong key (→ 0.0), re-requests
    on the first poll too, inflating the count by one.
    """
    hub = _FakeHub({"w": _worker({"screen": "x", "ts": 0.5})})
    await PollingCoordinator(hub).wait_for_guard(
        "w", expect_prompt_id="never", expect_regex=None, timeout_ms=5000, poll_interval_ms=20
    )
    # 5 polls: pre-loop request + re-requests on polls 2..5 (poll 1 skipped) = 5.
    assert len(clock.sleeps) == 5
    assert len(hub.requests) == 5


async def test_wait_for_guard_missing_ts_key_default_zero(clock: _FakeClock) -> None:
    """A snapshot lacking ``ts`` re-requests on every poll (default must be 0, not 1)."""
    hub = _FakeHub({"w": _worker({"screen": "no-ts"})})
    await PollingCoordinator(hub).wait_for_guard(
        "w", expect_prompt_id="never", expect_regex=None, timeout_ms=5000, poll_interval_ms=20
    )
    # 5 polls: pre-loop + re-request on every poll = 6.
    assert len(clock.sleeps) == 5
    assert len(hub.requests) == 6


async def test_wait_for_guard_none_snapshot_else_branch_zero(clock: _FakeClock) -> None:
    """A present worker with no snapshot re-requests every poll (else default 0, not 1)."""
    hub = _FakeHub({"w": _worker(None)})
    await PollingCoordinator(hub).wait_for_guard(
        "w", expect_prompt_id="never", expect_regex=None, timeout_ms=5000, poll_interval_ms=20
    )
    assert len(clock.sleeps) == 5
    assert len(hub.requests) == 6  # pre-loop + re-request on every poll
