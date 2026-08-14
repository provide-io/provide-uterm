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
