#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for the bug-fixes applied to connections.py (part 2).

Covers:
- hijack_released stops the backwards event scan.
- on_worker_empty fires when last browser disconnects; _background_tasks holds the task.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.hub.connections import _background_tasks
from provide.terminal.hijack.models import WorkerTermState


def _make_hub(**kwargs: Any) -> TermHub:
    return TermHub(**kwargs)


def _make_ws() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# hijack_released stops the backwards scan
# ---------------------------------------------------------------------------


class TestHijackReleasedStopsScan:
    """hijack_released in the event ring must stop the backwards scan.

    Kills mutmut_53 ("XXhijack_releasedXX") and mutmut_54 ("HIJACK_RELEASED").
    """

    async def test_hijack_released_after_old_expiry_stops_scan(self) -> None:
        """hijack_released stops the backwards scan before reaching an earlier expiry.

        Event sequence (chronological): hijack_acquired → hijack_owner_expired →
        hijack_released → snapshot.

        Backwards scan: snapshot → hijack_released (STOP, no expiry in this cycle) →
        resume_without_owner = True.

        With the mutated string, the scan continues past hijack_released and finds
        hijack_owner_expired → resume_without_owner = False (wrong).
        """
        from collections import deque

        hub = _make_hub()
        worker_ws = MagicMock()
        browser_ws = MagicMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[browser_ws] = "admin"
            st.events = deque(
                [
                    {"seq": 1, "ts": time.time(), "type": "hijack_acquired", "data": {}},
                    {"seq": 2, "ts": time.time(), "type": "hijack_owner_expired", "data": {}},
                    {"seq": 3, "ts": time.time(), "type": "hijack_released", "data": {}},
                    {"seq": 4, "ts": time.time(), "type": "snapshot", "data": {}},
                ],
                maxlen=2000,
            )
            st.hijack_owner = None
            st.hijack_session = None

        result = await hub.cleanup_browser_disconnect("w1", browser_ws, owned_hijack=True)

        assert result["resume_without_owner"] is True, (
            "hijack_released stops the scan; the expiry before it is for an earlier cycle "
            "— resume is still needed for this cycle"
        )


# ---------------------------------------------------------------------------
# on_worker_empty fires when last browser disconnects
# ---------------------------------------------------------------------------


class TestOnWorkerEmptyCallback:
    """cleanup_browser_disconnect fires on_worker_empty exactly once when browser_count
    reaches 0, and adds the task to _background_tasks to prevent premature GC.

    Kills:
    - mutmut_71: _background_tasks.add(None) — None pollutes the set instead of the task.
    - mutmut_72: task.add_done_callback(None) — raises TypeError immediately.
    - Provides coverage for the on_worker_empty firing path (mutmut_1/2/3 are
      equivalent mutants since browser_count initial value is always overwritten, but
      this test documents the expected behavior).
    """

    async def test_on_worker_empty_fires_with_last_browser(self) -> None:
        """on_worker_empty is called with the correct worker_id when last browser leaves."""
        called_with: list[str] = []

        async def _on_empty(worker_id: str) -> None:
            called_with.append(worker_id)

        hub = _make_hub()
        hub.on_worker_empty = _on_empty
        browser_ws = MagicMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = MagicMock()
            st.browsers[browser_ws] = "operator"

        await hub.cleanup_browser_disconnect("w1", browser_ws, owned_hijack=False)
        await asyncio.sleep(0)  # let the background task run

        assert called_with == ["w1"], "on_worker_empty must be called with the worker_id"

    async def test_on_worker_empty_not_fired_when_browsers_remain(self) -> None:
        """on_worker_empty must NOT fire if other browsers are still connected."""
        called: list[bool] = []

        async def _on_empty(worker_id: str) -> None:
            called.append(True)

        hub = _make_hub()
        hub.on_worker_empty = _on_empty
        browser1 = MagicMock()
        browser2 = MagicMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = MagicMock()
            st.browsers[browser1] = "operator"
            st.browsers[browser2] = "admin"

        await hub.cleanup_browser_disconnect("w1", browser1, owned_hijack=False)
        await asyncio.sleep(0)

        assert not called, "on_worker_empty must not fire while browsers remain"

    async def test_background_tasks_holds_task_not_none(self) -> None:
        """The task added to _background_tasks must be the asyncio.Task, not None.

        Kills mutmut_71: _background_tasks.add(None) adds None instead of the task.
        After the task completes, its done-callback discards itself from the set.
        None would remain permanently if the wrong value was added.
        """
        called: list[bool] = []

        async def _on_empty(worker_id: str) -> None:
            called.append(True)

        hub = _make_hub()
        hub.on_worker_empty = _on_empty
        browser_ws = MagicMock()

        async with hub._lock:
            st = hub._workers.setdefault("w2", WorkerTermState())
            st.worker_ws = MagicMock()
            st.browsers[browser_ws] = "operator"

        # Take a snapshot of the set before to detect any lingering None.
        _background_tasks.discard(None)  # ensure clean state
        await hub.cleanup_browser_disconnect("w2", browser_ws, owned_hijack=False)
        await asyncio.sleep(0)  # let task complete and run done-callback

        assert None not in _background_tasks, (
            "_background_tasks must not contain None — "
            "mutmut_71 adds None instead of the task, leaving it in the set permanently"
        )
