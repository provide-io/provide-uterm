#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for provide.uterm.io (part 3).

Surgical assertions that pin the exact timeout-arithmetic, dict-key, and
callback-argument behaviour of ``PromptWaiter.wait_for_prompt`` and its
helpers, killing the remaining mutmut survivors that the weaker
"completes without hanging" tests in part 2 could not.

A deterministic fake ``time.monotonic`` is used so the timeout/remaining
arithmetic is fully controllable and the exact ``wait_for_update`` timeout_ms
can be asserted. The fake clock RAISES when its scripted ticks are exhausted,
so a mutant that runs an extra loop iteration fails fast (killed) instead of
spinning into a mutmut timeout.
"""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm import io as io_mod
from provide.uterm.io import PromptWaiter


def _prompt_snapshot(
    *,
    prompt_id: str = "main_menu",
    input_type: str = "multi_key",
    screen: str = "Choose:",
    is_idle: bool = True,
    screen_hash: str = "abc123",
    captured_at: float = 1.5,
    kv_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detected: dict[str, Any] = {
        "prompt_id": prompt_id,
        "input_type": input_type,
        "is_idle": is_idle,
    }
    if kv_data is not None:
        detected["kv_data"] = kv_data
    return {
        "screen": screen,
        "screen_hash": screen_hash,
        "captured_at": captured_at,
        "prompt_detected": detected,
    }


def _waiter(snapshots: list[dict[str, Any]], wait_calls: list[int | None]) -> PromptWaiter:
    session = MagicMock()
    session.is_connected = MagicMock(return_value=True)
    session.snapshot = MagicMock(side_effect=snapshots)

    async def _wait(*, timeout_ms: int | None) -> bool:
        wait_calls.append(timeout_ms)
        return True

    session.wait_for_update = _wait
    session.send = AsyncMock()
    return PromptWaiter(session)


# ---------------------------------------------------------------------------
# _check_prompt_filters: expected_mismatch + callback_reject wait timeouts
# (mutmut_9/12/16/23/25/26)
# ---------------------------------------------------------------------------


class TestCheckPromptFiltersTimeouts:
    async def test_expected_mismatch_wait_uses_exact_interval_ms(self) -> None:
        """Mismatch branch must wait int(read_interval_sec * 1000) ms exactly.

        read_interval_ms=1000 -> read_interval_sec=1.0 -> wait 1000 ms.
        Distinguishes *1000 (1000) from None, /1000 (0), and *1001 (1001).
        """
        wait_calls: list[int | None] = []
        waiter = _waiter(
            [_prompt_snapshot(prompt_id="login"), _prompt_snapshot(prompt_id="main_menu")],
            wait_calls,
        )
        result = await waiter.wait_for_prompt(
            timeout_ms=5000,
            read_interval_ms=1000,
            expected_prompt_id="main_menu",
        )
        assert result["prompt_id"] == "main_menu"
        # Exactly one rejection wait happened before the match.
        assert wait_calls == [1000]

    async def test_callback_reject_wait_uses_exact_interval_ms(self) -> None:
        """callback_reject branch must wait int(read_interval_sec * 1000) ms exactly."""
        wait_calls: list[int | None] = []
        waiter = _waiter(
            [_prompt_snapshot(), _prompt_snapshot()],
            wait_calls,
        )
        calls = {"n": 0}

        def on_detected(_data: dict[str, Any]) -> bool:
            calls["n"] += 1
            return calls["n"] != 1  # reject first, accept second

        result = await waiter.wait_for_prompt(
            timeout_ms=5000,
            read_interval_ms=1000,
            on_prompt_detected=on_detected,
        )
        assert result["prompt_id"] == "main_menu"
        assert wait_calls == [1000]

    async def test_on_prompt_detected_receives_detected_full_dict(self) -> None:
        """on_prompt_detected must be called with detected_full, never None (mutmut_16)."""
        wait_calls: list[int | None] = []
        waiter = _waiter([_prompt_snapshot(screen="HELLO")], wait_calls)
        seen: list[Any] = []

        def on_detected(data: Any) -> bool:
            seen.append(data)
            return True

        await waiter.wait_for_prompt(timeout_ms=5000, on_prompt_detected=on_detected)
        assert len(seen) == 1
        assert seen[0] is not None
        assert isinstance(seen[0], dict)
        assert seen[0]["screen"] == "HELLO"


# ---------------------------------------------------------------------------
# _wait_if_not_idle: max(1, ...) lower bound (mutmut_31)
# ---------------------------------------------------------------------------


class TestWaitIfNotIdleLowerBound:
    async def test_wait_ms_floor_is_one_not_two(self) -> None:
        """wait_ms = int(max(1, ...)) — a sub-1ms computed value floors to 1, not 2.

        seconds_until_idle=0.0 makes the product ~0, so the floor (max's first
        arg) is observable: orig -> 1, mutant max(2,..) -> 2.
        """
        wait_calls: list[int | None] = []
        # Non-idle first, then idle so the loop terminates.
        snaps = [
            _prompt_snapshot(is_idle=False),
            _prompt_snapshot(is_idle=True),
        ]
        session = MagicMock()
        session.is_connected = MagicMock(return_value=True)
        session.snapshot = MagicMock(side_effect=snaps)
        session.seconds_until_idle = MagicMock(return_value=0.0)

        async def _wait(*, timeout_ms: int | None) -> bool:
            wait_calls.append(timeout_ms)
            return True

        session.wait_for_update = _wait
        waiter = PromptWaiter(session)
        # large grace ratio so the non-idle prompt is skipped (waits).
        await waiter.wait_for_prompt(timeout_ms=5000, require_idle=True, idle_grace_ratio=0.9)
        # The not-idle wait floors to 1 ms (0.0s product -> max(1, ~0) -> 1).
        assert wait_calls[0] == 1


# ---------------------------------------------------------------------------
# Deterministic-clock arithmetic on the no-prompt branch:
#   timeout_sec = timeout_ms / 1000.0
#   read_interval_sec = read_interval_ms / 1000.0
#   while monotonic() - start < timeout_sec
#   remaining = max(0, timeout_sec - (monotonic() - start))
#   wait_for_update(timeout_ms=int(min(read_interval_sec, remaining) * 1000))
# ---------------------------------------------------------------------------


class _Clock:
    """Monotonic clock returning a scripted sequence; raises when exhausted.

    Raising on over-read means a mutant that adds an extra loop iteration
    fails deterministically (killed) rather than spinning forever (timeout).
    """

    def __init__(self, values: list[float]) -> None:
        self._values = values
        self._idx = 0

    def __call__(self) -> float:
        if self._idx >= len(self._values):
            raise AssertionError("clock exhausted: mutant took an extra tick")
        v = self._values[self._idx]
        self._idx += 1
        return v


def _patch_clock(monkeypatch: pytest.MonkeyPatch, values: list[float]) -> None:
    """Rebind *io's* ``time`` reference to a scripted-monotonic namespace.

    Rebinding ``io_mod.time`` (rather than mutating ``time.monotonic`` on the
    shared module) keeps the asyncio event loop's own ``time.monotonic`` reads
    out of the scripted sequence, so the tick count is exactly io's reads.
    """
    monkeypatch.setattr(io_mod, "time", types.SimpleNamespace(monotonic=_Clock(values)))


def _no_prompt_session(wait_calls: list[int | None], *, snapshots: int = 1) -> MagicMock:
    """Session whose snapshot has no 'prompt_detected' (always hits wait branch).

    ``snapshots`` bounds how many polls are allowed before StopIteration, so a
    mutant that loops too many times is killed instead of hanging.
    """
    session = MagicMock()
    session.is_connected = MagicMock(return_value=True)
    session.snapshot = MagicMock(side_effect=[{"screen": "busy"}] * snapshots)

    async def _wait(*, timeout_ms: int | None) -> bool:
        wait_calls.append(timeout_ms)
        return True

    session.wait_for_update = _wait
    return session


class TestNoPromptWaitArithmetic:
    """Pin the no-prompt wait timeout and its `remaining` subexpression.

    Clock call order per run: start_mono; then per iteration the loop-cond
    read and (on the no-prompt branch) the `remaining` read. start_mono is
    deliberately NON-ZERO so `monotonic() - start` differs from
    `monotonic() + start` (kills the +/- direction mutant).
    """

    async def test_remaining_dominates_and_scales_by_1000(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """remaining (9.0s) < read_interval (20.0s): wait = 9.0 * 1000 = 9000 ms.

        start=100.0 (non-zero). timeout_ms=10000 -> timeout_sec=10.0.
        Kills: *1000<->/1000<->None (9000 vs 9 vs None), *1000<->*1001
        (9000 vs 9009), and the `(mono - start)` vs `(mono + start)` flip
        (9000 vs max(0, 10 - 201) = 0).
        """
        wait_calls: list[int | None] = []
        session = _no_prompt_session(wait_calls, snapshots=1)
        # start=100.0; loop-cond=100.0 (elapsed 0 < 10 -> enter);
        # remaining-read=101.0 (remaining = max(0, 10 - 1) = 9.0);
        # loop-cond=111.0 (elapsed 11 < 10 -> exit).
        _patch_clock(monkeypatch, [100.0, 100.0, 101.0, 111.0])
        waiter = PromptWaiter(session)
        with pytest.raises(TimeoutError):
            await waiter.wait_for_prompt(timeout_ms=10000, read_interval_ms=20000)
        assert wait_calls == [9000]

    async def test_interval_dominates_and_scales_by_1000(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """read_interval (0.25s) < remaining: wait = 0.25 * 1000 = 250 ms.

        Kills the read_interval_sec division mutants (read_interval_ms * 1000
        and / 1001) which would otherwise leave the interval term wrong.
        """
        wait_calls: list[int | None] = []
        session = _no_prompt_session(wait_calls, snapshots=1)
        # start=100.0; loop-cond=100.0 (enter); remaining-read=100.1
        # (remaining = max(0, 10 - 0.1) = 9.9); loop-cond=111.0 (exit).
        _patch_clock(monkeypatch, [100.0, 100.0, 100.1, 111.0])
        waiter = PromptWaiter(session)
        with pytest.raises(TimeoutError):
            await waiter.wait_for_prompt(timeout_ms=10000, read_interval_ms=250)
        assert wait_calls == [250]

    async def test_remaining_floor_is_zero_not_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """remaining = max(0, ...): at exact expiry it floors to 0 ms, not 1000 ms.

        Mutant max(1, ...) would yield min(20.0, 1)*1000 = 1000.
        """
        wait_calls: list[int | None] = []
        session = _no_prompt_session(wait_calls, snapshots=1)
        # start=100.0; loop-cond=100.0 (enter); remaining-read=110.0
        # (remaining = max(0, 10 - 10) = 0); loop-cond=110.0 (exit).
        _patch_clock(monkeypatch, [100.0, 100.0, 110.0, 110.0])
        waiter = PromptWaiter(session)
        with pytest.raises(TimeoutError):
            await waiter.wait_for_prompt(timeout_ms=10000, read_interval_ms=20000)
        assert wait_calls == [0]


class TestTimeoutSecScaling:
    """timeout_sec = timeout_ms / 1000.0 (mutant '* 1000' and '/ 1001')."""

    async def test_timeout_sec_divides_by_1000(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """timeout_ms=1000 must yield timeout_sec == 1.0.

        The loop runs exactly once: at elapsed 0.5 (< 1.0) it polls + waits,
        then at elapsed 1.0 it exits. Only one snapshot is provided, so a
        '* 1000' mutant (timeout_sec = 1_000_000) takes a second tick and
        trips the exhausted-clock guard; '/ 1001' (timeout_sec ≈ 0.999) would
        exit before the first poll, producing zero waits.
        """
        wait_calls: list[int | None] = []
        session = _no_prompt_session(wait_calls, snapshots=1)
        # start=100.0; loop-cond=100.5 (elapsed 0.5 < 1.0 -> enter);
        # remaining-read=100.5 (remaining = max(0, 1.0 - 0.5) = 0.5);
        # loop-cond=101.0 (elapsed 1.0 < 1.0 -> exit).
        _patch_clock(monkeypatch, [100.0, 100.5, 100.5, 101.0])
        waiter = PromptWaiter(session)
        with pytest.raises(TimeoutError):
            await waiter.wait_for_prompt(timeout_ms=1000, read_interval_ms=5000)
        # remaining 0.5s -> 500 ms; proves the 1.0s timeout scale.
        assert wait_calls == [500]


class TestLoopConditionBoundary:
    """while monotonic() - start < timeout_sec  (strict '<' vs '<=')."""

    async def test_loop_exits_at_exact_timeout_no_poll(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """At elapsed == timeout_sec the strict-< loop must NOT run, so snapshot
        is never consulted and TimeoutError is raised.

        A '<=' mutant would enter the loop and poll; with zero snapshots
        provided that poll raises StopIteration -> killed.
        """
        session = MagicMock()
        session.is_connected = MagicMock(return_value=True)
        session.snapshot = MagicMock(side_effect=[])  # any poll -> StopIteration

        async def _wait(*, timeout_ms: int | None) -> bool:
            return True

        session.wait_for_update = _wait
        # start=100.0; loop-cond=101.0 (elapsed 1.0; '<' 1.0 -> exit, '<=' -> enter).
        _patch_clock(monkeypatch, [100.0, 101.0])
        waiter = PromptWaiter(session)
        with pytest.raises(TimeoutError):
            await waiter.wait_for_prompt(timeout_ms=1000)
        session.snapshot.assert_not_called()


# ---------------------------------------------------------------------------
# detected_full["screen"] assignment key+value (mutmut_33/34/35)
# ---------------------------------------------------------------------------


class TestDetectedFullScreenAssignment:
    async def test_on_prompt_seen_receives_screen_under_screen_key(self) -> None:
        """detected_full['screen'] = screen — exact key 'screen' and the screen value.

        The callback payload must expose key 'screen' (not 'XXscreenXX'/'SCREEN')
        carrying the live screen text (not None).
        """
        wait_calls: list[int | None] = []
        waiter = _waiter([_prompt_snapshot(screen="LIVE-SCREEN")], wait_calls)
        seen: list[dict[str, Any]] = []
        waiter_result = await waiter.wait_for_prompt(
            timeout_ms=5000,
            on_prompt_seen=seen.append,
        )
        assert waiter_result["prompt_id"] == "main_menu"
        assert len(seen) == 1
        payload = seen[0]
        assert payload["screen"] == "LIVE-SCREEN"
        assert "XXscreenXX" not in payload
        assert "SCREEN" not in payload
