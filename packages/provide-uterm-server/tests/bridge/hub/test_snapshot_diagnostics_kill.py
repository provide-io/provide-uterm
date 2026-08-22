#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Kill suite for the two snapshot diagnostics added by 2163d535.

That commit added two warnings — ``snapshot_req_undelivered`` in
PresenceManager.request_snapshot and ``snapshot_wait_timeout`` in
PollingCoordinator.wait_for_snapshot — with no tests, and the full-perimeter
mutation gate has been red on them since. Both are pure observability: nothing
downstream reads them, so every behavioural test in the repo passes with the
whole block deleted, its condition inverted, or its fields nulled.

A diagnostic nobody asserts is a diagnostic that silently rots into a lie,
which is worse than not having one — the whole point of these two is that a
wedged worker gets *recorded*. So the assertions here are on the CALL: the
event name, and every keyword argument by value.

Each test names the mutant family it exists to kill.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.server.bridge.hub import TermHub, polling_service, presence
from provide.uterm.server.bridge.models import WorkerTermState


def _warnings(log: MagicMock, event: str) -> list[dict[str, Any]]:
    """Every kwargs dict logged for *event*."""
    return [c.kwargs for c in log.warning.call_args_list if c.args and c.args[0] == event]


# ---------------------------------------------------------------------------
# PresenceManager.request_snapshot — snapshot_req_undelivered
# ---------------------------------------------------------------------------


class TestSnapshotReqUndelivered:
    """Kills: `if not delivered` inversion, the event name, and each kwarg.

    `registered` and `worker_ws` are computed from two independent `is not None`
    checks joined by `and`; the three states below pin every arm, including the
    and/or swap that only differs when exactly one side is false.
    """

    @staticmethod
    def _hub(*, delivered: bool) -> TermHub:
        hub = TermHub()
        hub.send_worker = AsyncMock(return_value=delivered)  # type: ignore[method-assign]
        return hub

    async def test_a_delivered_request_logs_nothing(self) -> None:
        """Kills `if not delivered:` -> `if delivered:`."""
        hub = self._hub(delivered=True)
        hub.registry._workers["bot1"] = WorkerTermState()

        with patch.object(presence, "logger", MagicMock()) as log:
            await hub.request_snapshot("bot1")

        assert _warnings(log, "snapshot_req_undelivered") == []

    async def test_undelivered_to_a_registered_worker_with_a_socket(self) -> None:
        """registered=True, worker_ws=True — both arms of the `and` true."""
        hub = self._hub(delivered=False)
        state = WorkerTermState()
        state.worker_ws = AsyncMock()
        hub.registry._workers["bot1"] = state

        with patch.object(presence, "logger", MagicMock()) as log:
            await hub.request_snapshot("bot1")

        assert _warnings(log, "snapshot_req_undelivered") == [
            {"worker_id": "bot1", "registered": True, "worker_ws": True}
        ]

    async def test_undelivered_to_a_registered_worker_without_a_socket(self) -> None:
        """registered=True, worker_ws=False.

        This is the case that separates `and` from `or`: exactly one side is
        false, so an or-mutant reports worker_ws=True here and nowhere else.
        """
        hub = self._hub(delivered=False)
        state = WorkerTermState()
        state.worker_ws = None
        hub.registry._workers["bot1"] = state

        with patch.object(presence, "logger", MagicMock()) as log:
            await hub.request_snapshot("bot1")

        assert _warnings(log, "snapshot_req_undelivered") == [
            {"worker_id": "bot1", "registered": True, "worker_ws": False}
        ]

    async def test_undelivered_to_an_unregistered_worker(self) -> None:
        """registered=False — kills the `st is not None` -> `is None` flip.

        Also pins that the lookup uses the requested id: a mutant passing None
        to registry.get() would find nothing here AND in the tests above, so it
        is the contrast between this case and those that kills it.
        """
        hub = self._hub(delivered=False)

        with patch.object(presence, "logger", MagicMock()) as log:
            await hub.request_snapshot("ghost")

        assert _warnings(log, "snapshot_req_undelivered") == [
            {"worker_id": "ghost", "registered": False, "worker_ws": False}
        ]

    async def test_the_request_itself_is_still_sent(self) -> None:
        """The diagnostic must not replace the send it reports on."""
        hub = self._hub(delivered=False)

        with patch.object(presence, "logger", MagicMock()):
            await hub.request_snapshot("bot1")

        hub.send_worker.assert_awaited_once()  # type: ignore[attr-defined]
        worker_id, frame = hub.send_worker.await_args.args  # type: ignore[attr-defined]
        assert worker_id == "bot1"
        assert frame["type"] == "snapshot_req"
        assert frame["req_id"]  # a correlation id is always minted


# ---------------------------------------------------------------------------
# PollingCoordinator.wait_for_snapshot — snapshot_wait_timeout
# ---------------------------------------------------------------------------


_FROZEN_NOW = 1_700_000_000.0


class TestSnapshotWaitTimeout:
    """Kills: the stale_age arithmetic, its rounding, and each kwarg.

    time.time is frozen so cached_age_s is an exact expected number rather than
    "about five seconds" — an approximate assertion cannot kill a swapped
    subtraction or a changed rounding precision.

    asyncio.sleep is a no-op so the deadline is reached immediately: these
    assertions are about what gets LOGGED on timeout, and paying the real
    interval to reach it only makes the suite slow and load-sensitive.
    """

    @staticmethod
    def _hub() -> TermHub:
        hub = TermHub()
        hub.request_snapshot = AsyncMock()  # type: ignore[method-assign]
        return hub

    async def test_timeout_with_a_stale_cached_snapshot_reports_its_age(self) -> None:
        """Kills `req_ts - ts` swaps and `round(..., 3)` precision changes."""
        hub = self._hub()
        state = WorkerTermState()
        state.last_snapshot = {"screen": "stale", "ts": _FROZEN_NOW - 5.0}
        hub.registry._workers["bot1"] = state

        with (
            patch.object(polling_service.time, "time", return_value=_FROZEN_NOW),
            patch.object(polling_service.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(polling_service, "logger", MagicMock()) as log,
        ):
            result = await hub.wait_for_snapshot("bot1", timeout_ms=50)

        assert result is None
        assert _warnings(log, "snapshot_wait_timeout") == [
            {"worker_id": "bot1", "timeout_ms": 50, "had_cached": True, "cached_age_s": 5.0}
        ]

    async def test_timeout_with_no_cached_snapshot_reports_no_age(self) -> None:
        """Kills the `if snap is not None` guard and the `stale_age = None` init."""
        hub = self._hub()
        hub.registry._workers["bot1"] = WorkerTermState()

        with (
            patch.object(polling_service.time, "time", return_value=_FROZEN_NOW),
            patch.object(polling_service.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(polling_service, "logger", MagicMock()) as log,
        ):
            result = await hub.wait_for_snapshot("bot1", timeout_ms=50)

        assert result is None
        assert _warnings(log, "snapshot_wait_timeout") == [
            {"worker_id": "bot1", "timeout_ms": 50, "had_cached": False, "cached_age_s": None}
        ]

    async def test_a_non_numeric_cached_ts_raises_from_the_poll_loop(self) -> None:
        """Documents that wait_for_snapshot does NOT tolerate a non-numeric ts.

        The timeout block wraps its float() in contextlib.suppress(TypeError,
        ValueError), which reads as "a junk ts is survivable". It is not: the
        loop above compares `snap.get("ts", 0) > req_ts` unguarded, so a string
        ts raises before the timeout block is ever reached. That suppress is
        therefore unreachable for the input it was written for.

        Asserted as it behaves, not as it was meant to: pinning the raise means
        a later fix that makes the poll tolerant has to come here and say so.
        """
        hub = self._hub()
        state = WorkerTermState()
        state.last_snapshot = {"screen": "stale", "ts": "not-a-number"}
        hub.registry._workers["bot1"] = state

        with (
            patch.object(polling_service.time, "time", return_value=_FROZEN_NOW),
            patch.object(polling_service.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(polling_service, "logger", MagicMock()) as log,
            pytest.raises(TypeError),
        ):
            await hub.wait_for_snapshot("bot1", timeout_ms=50)

        assert _warnings(log, "snapshot_wait_timeout") == []

    async def test_the_timeout_ms_logged_is_the_one_requested(self) -> None:
        """Kills a hard-coded or defaulted timeout_ms in the warning."""
        hub = self._hub()
        hub.registry._workers["bot1"] = WorkerTermState()

        with (
            patch.object(polling_service.time, "time", return_value=_FROZEN_NOW),
            patch.object(polling_service.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(polling_service, "logger", MagicMock()) as log,
        ):
            await hub.wait_for_snapshot("bot1", timeout_ms=250)

        assert _warnings(log, "snapshot_wait_timeout")[0]["timeout_ms"] == 250

    async def test_a_fresh_snapshot_logs_no_timeout(self) -> None:
        """The warning must fire ONLY on timeout, not on every wait."""
        hub = self._hub()
        state = WorkerTermState()
        state.last_snapshot = {"screen": "fresh", "ts": _FROZEN_NOW + 1.0}
        hub.registry._workers["bot1"] = state

        with (
            patch.object(polling_service.time, "time", return_value=_FROZEN_NOW),
            patch.object(polling_service.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(polling_service, "logger", MagicMock()) as log,
        ):
            result = await hub.wait_for_snapshot("bot1", timeout_ms=50)

        assert result is not None
        assert _warnings(log, "snapshot_wait_timeout") == []

    async def test_a_vanished_worker_returns_without_logging_a_timeout(self) -> None:
        """Registry miss returns None from inside the loop — not the timeout path."""
        hub = self._hub()

        with (
            patch.object(polling_service.time, "time", return_value=_FROZEN_NOW),
            patch.object(polling_service.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(polling_service, "logger", MagicMock()) as log,
        ):
            result = await hub.wait_for_snapshot("ghost", timeout_ms=50)

        assert result is None
        assert _warnings(log, "snapshot_wait_timeout") == []


async def test_asyncio_is_not_shadowed() -> None:
    """Guard for the patches above: they patch polling_service's own asyncio."""
    assert polling_service.asyncio is asyncio


# ---------------------------------------------------------------------------
# PollingCoordinator._snapshot_is_fresh — the event_seq freshness rewrite
# ---------------------------------------------------------------------------


class TestSnapshotIsFresh:
    """Kills the freshness predicate added when wait_for_snapshot was rewritten.

    Freshness moved from a wall-clock proxy to a monotonic per-worker event_seq
    because the old comparison discarded the one frame the caller needed: a push
    landing microseconds BEFORE the poll asked was, by ts, "not newer than the
    request". The predicate is a static method with no I/O, so every arm is
    reachable directly — and it is worth pinning hard, because a wrong answer
    here is invisible (the caller just waits out its window and reports a stall
    against a worker that published correctly).
    """

    @staticmethod
    def _fresh(snapshot: dict[str, Any], *, req_ts: float = 100.0, after: int | None = None) -> bool:
        return polling_service.PollingCoordinator._snapshot_is_fresh(snapshot, req_ts=req_ts, after_event_seq=after)

    def test_event_seq_strictly_greater_is_fresh(self) -> None:
        """Kills `>` -> `>=` on the seq comparison."""
        assert self._fresh({"event_seq": 6}, after=5) is True
        assert self._fresh({"event_seq": 5}, after=5) is False
        assert self._fresh({"event_seq": 4}, after=5) is False

    def test_a_bool_event_seq_is_not_a_sequence_number(self) -> None:
        """Kills dropping `not isinstance(seq, bool)`.

        bool subclasses int, so True would otherwise read as seq==1 and answer
        "fresh" against after_event_seq=0 — a JSON `true` in that field would
        silently release every waiter.
        """
        assert self._fresh({"event_seq": True}, after=0) is False
        assert self._fresh({"event_seq": False}, after=-1) is False

    def test_a_missing_or_non_integer_event_seq_is_not_fresh(self) -> None:
        """Kills the isinstance(seq, int) guard."""
        assert self._fresh({}, after=5) is False
        assert self._fresh({"event_seq": None}, after=5) is False
        assert self._fresh({"event_seq": "7"}, after=5) is False
        assert self._fresh({"event_seq": 7.5}, after=5) is False

    def test_after_event_seq_zero_still_uses_the_seq_path(self) -> None:
        """Kills `if after_event_seq is not None` -> a truthiness test.

        Zero is a legitimate "I have seen frame 0"; a falsy check would fall
        through to the wall-clock branch and answer a different question.
        """
        assert self._fresh({"event_seq": 1, "ts": 0.0}, req_ts=100.0, after=0) is True
        # ts is ancient, so the wall-clock branch would say False — proving the
        # seq path is the one being taken.
        assert self._fresh({"event_seq": 0, "ts": 0.0}, req_ts=100.0, after=0) is False

    def test_without_after_event_seq_it_falls_back_to_wall_clock(self) -> None:
        """Kills `>` -> `>=` and the get default on the ts branch."""
        assert self._fresh({"ts": 100.5}, req_ts=100.0) is True
        assert self._fresh({"ts": 100.0}, req_ts=100.0) is False
        assert self._fresh({"ts": 99.5}, req_ts=100.0) is False
        # No ts at all defaults to 0, which is never newer than a real req_ts.
        assert self._fresh({}, req_ts=100.0) is False

    def test_the_wall_clock_branch_ignores_event_seq(self) -> None:
        """The two branches must not blend: no after_event_seq means ts decides."""
        assert self._fresh({"event_seq": 999, "ts": 99.0}, req_ts=100.0) is False


class TestWaitForSnapshotDeadline:
    """Kills the loop restructure: freshness is checked BEFORE the deadline.

    The rewrite moved from `while monotonic() < end` to `while True` with the
    deadline break AFTER the freshness check, so an already-fresh snapshot is
    returned even when the budget is already spent. A mutant that restores the
    old order turns that into a timeout.
    """

    async def test_an_already_fresh_snapshot_wins_a_zero_budget(self) -> None:
        hub = TermHub()
        hub.request_snapshot = AsyncMock()  # type: ignore[method-assign]
        state = WorkerTermState()
        state.last_snapshot = {"screen": "fresh", "ts": _FROZEN_NOW + 1.0}
        hub.registry._workers["bot1"] = state

        with (
            patch.object(polling_service.time, "time", return_value=_FROZEN_NOW),
            patch.object(polling_service.asyncio, "sleep", new_callable=AsyncMock) as slept,
            patch.object(polling_service, "logger", MagicMock()) as log,
        ):
            result = await hub.wait_for_snapshot("bot1", timeout_ms=0)

        assert result is not None
        assert result["screen"] == "fresh"
        slept.assert_not_awaited()
        assert _warnings(log, "snapshot_wait_timeout") == []


# ---------------------------------------------------------------------------
# wait_for_snapshot — the timeout block reached with a junk or absent ts
# ---------------------------------------------------------------------------


class TestSnapshotWaitTimeoutStaleAgeEdges:
    """Kills the suppress arguments, the rounding precision, and the ts default.

    The class above could not reach any of them. Its snapshots all carry a
    numeric ``ts``, so ``snap.get("ts", 0)``'s default never applies and
    ``float()`` never raises; and its one junk-ts test is killed by the poll
    loop before the timeout block runs, because the wall-clock proxy compares
    ``snap.get("ts", 0) > req_ts`` unguarded.

    ``after_event_seq`` is the way in. With it set and the snapshot carrying an
    int ``event_seq``, _snapshot_is_fresh answers from the sequence alone and
    never touches ``ts`` — so a snapshot whose ``ts`` is junk, or missing
    entirely, reaches the timeout block intact and the arithmetic there is
    finally observable.
    """

    @staticmethod
    def _hub_with(snapshot: dict[str, Any]) -> TermHub:
        hub = TermHub()
        hub.request_snapshot = AsyncMock()  # type: ignore[method-assign]
        state = WorkerTermState()
        state.last_snapshot = snapshot
        hub.registry._workers["bot1"] = state
        return hub

    async def _timeout_warning(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        hub = self._hub_with(snapshot)
        with (
            patch.object(polling_service.time, "time", return_value=_FROZEN_NOW),
            patch.object(polling_service.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(polling_service, "logger", MagicMock()) as log,
        ):
            result = await hub.wait_for_snapshot("bot1", timeout_ms=50, after_event_seq=10)
        assert result is None
        return _warnings(log, "snapshot_wait_timeout")[0]

    async def test_the_age_keeps_exactly_three_decimals(self) -> None:
        """Kills `round(..., 3)` -> `4`, -> `None`, and the dropped precision arg.

        5.0 cannot catch any of them: round(5.0, None) is 5, and 5 == 5.0. The
        age here has more decimals than the precision keeps, so each variant
        produces a different number — 1.235 against 1.2346 and 1.
        """
        warning = await self._timeout_warning({"event_seq": 5, "ts": _FROZEN_NOW - 1.23456789})
        assert warning["cached_age_s"] == 1.235

    async def test_a_snapshot_with_no_ts_ages_from_zero(self) -> None:
        """Kills the `snap.get("ts", 0)` default -> `1`, -> `None`, -> dropped.

        With the default at 0 the age is the whole wall clock. A default of 1
        shifts it by exactly one second; a default of None (or none at all)
        makes float() raise, which the suppress swallows into a None age.
        """
        warning = await self._timeout_warning({"event_seq": 5})
        assert warning["had_cached"] is True
        assert warning["cached_age_s"] == round(_FROZEN_NOW, 3)

    async def test_a_string_ts_is_suppressed_into_no_age(self) -> None:
        """Kills `suppress(TypeError, )` — dropping ValueError from the tuple."""
        warning = await self._timeout_warning({"event_seq": 5, "ts": "not-a-number"})
        assert warning["had_cached"] is True
        assert warning["cached_age_s"] is None

    async def test_a_none_ts_is_suppressed_into_no_age(self) -> None:
        """Kills `suppress(ValueError)` — dropping TypeError from the tuple.

        Sibling of the test above and not redundant with it: float() raises
        ValueError for a string and TypeError for None, so one test each is
        what it takes to pin both members of the tuple.
        """
        warning = await self._timeout_warning({"event_seq": 5, "ts": None})
        assert warning["had_cached"] is True
        assert warning["cached_age_s"] is None


class TestSnapshotWaitTimeoutMetric:
    """Kills every mutation of the `snapshot_wait_timeout` counter increment.

    Nothing in the repo asserted this metric, so the whole call was free to be
    mutated: the increment could be doubled, its attributes emptied, dropped,
    or the worker_id key renamed, and every test still passed. A counter that
    reports the wrong worker is worse than one that reports nothing, because it
    sends whoever is debugging the wedge to the wrong session.
    """

    async def test_the_timeout_counter_records_one_for_this_worker(self) -> None:
        hub = TermHub()
        hub.request_snapshot = AsyncMock()  # type: ignore[method-assign]
        hub.registry._workers["bot1"] = WorkerTermState()
        counter = MagicMock()

        with (
            patch.object(polling_service.time, "time", return_value=_FROZEN_NOW),
            patch.object(polling_service.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(polling_service, "logger", MagicMock()),
            patch.object(polling_service.snapshot_metrics, "snapshot_wait_timeout", counter),
        ):
            await hub.wait_for_snapshot("bot1", timeout_ms=50)

        assert counter.add.call_args_list == [((1, {"worker_id": "bot1"}),)]

    async def test_no_timeout_leaves_the_counter_alone(self) -> None:
        """The counter must track timeouts, not calls."""
        hub = TermHub()
        hub.request_snapshot = AsyncMock()  # type: ignore[method-assign]
        state = WorkerTermState()
        state.last_snapshot = {"screen": "fresh", "ts": _FROZEN_NOW + 1.0}
        hub.registry._workers["bot1"] = state
        counter = MagicMock()

        with (
            patch.object(polling_service.time, "time", return_value=_FROZEN_NOW),
            patch.object(polling_service.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(polling_service, "logger", MagicMock()),
            patch.object(polling_service.snapshot_metrics, "snapshot_wait_timeout", counter),
        ):
            await hub.wait_for_snapshot("bot1", timeout_ms=50)

        assert counter.add.call_args_list == []
