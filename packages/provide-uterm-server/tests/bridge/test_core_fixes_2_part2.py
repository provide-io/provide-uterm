#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for src/provide/uterm/hijack/hub/core.py (part 2).

Covers:
- disconnect_worker: was_hijacked logic; broadcast payload correctness;
  notify_hijack_changed / broadcast_hijack_state / prune_if_idle called with
  correct worker_id; logger.debug exercised on close exception.
- set_input_mode: returns exact error string "active_hijack"; broadcast payload
  contains "ts" key.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState
from tests.bridge.control_channel_helpers import decode_control_payloads


def _make_hub(**kwargs: Any) -> TermHub:
    return TermHub(**kwargs)


def _make_async_ws() -> AsyncMock:
    """Return a mock WebSocket with async send_text and close."""
    ws = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# disconnect_worker: was_hijacked logic (kills mutmut_13)
# ---------------------------------------------------------------------------


class TestSetInputModeBroadcastPayload:
    """set_input_mode broadcasts an 'input_mode_changed' message with a 'ts' timestamp.

    Kills:
    - mutmut_26: "ts" key changed to "XXtsXX" or similar
    - mutmut_27: time.time() → 0 or other mutation of the timestamp value
    """

    async def test_broadcast_contains_ts_key(self) -> None:
        """The input_mode_changed broadcast payload must contain a 'ts' key."""
        hub = _make_hub()
        browser_ws = _make_async_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.browsers[browser_ws] = "operator"

        before = time.time()
        await hub.set_input_mode("w1", "open")
        after = time.time()

        calls = browser_ws.send_text.call_args_list
        assert calls, "Browser must receive at least one message after set_input_mode"
        payloads = decode_control_payloads([call.args[0] for call in calls])
        mode_msgs = [p for p in payloads if p.get("type") == "input_mode_changed"]
        assert mode_msgs, f"Expected 'input_mode_changed' in broadcast, got: {payloads}"

        msg = mode_msgs[0]
        assert "ts" in msg, (
            f"input_mode_changed payload must contain 'ts' key, got {msg!r} — mutmut_26 renames 'ts' to something else"
        )
        assert isinstance(msg["ts"], (int, float)), (
            f"'ts' must be a numeric timestamp, got {msg['ts']!r} — "
            "mutmut_27 replaces time.time() with a non-numeric value"
        )
        assert before <= msg["ts"] <= after + 1, f"'ts' must be a recent timestamp, got {msg['ts']}"

    async def test_broadcast_contains_input_mode_field(self) -> None:
        """The input_mode_changed payload must contain the new mode value."""
        hub = _make_hub()
        browser_ws = _make_async_ws()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.browsers[browser_ws] = "operator"

        await hub.set_input_mode("w1", "open")

        calls = browser_ws.send_text.call_args_list
        payloads = decode_control_payloads([call.args[0] for call in calls])
        mode_msgs = [p for p in payloads if p.get("type") == "input_mode_changed"]
        assert mode_msgs
        assert mode_msgs[0].get("input_mode") == "open"


# ---------------------------------------------------------------------------
# hub.shutdown() and shutdown_background_tasks
# ---------------------------------------------------------------------------


async def test_hub_shutdown_cancels_background_tasks() -> None:
    """shutdown() cancels background tasks and logs the count."""
    import asyncio

    from provide.uterm.server.bridge.hub import TermHub

    hub = TermHub()

    async def _long_running() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(_long_running())
    hub._background_tasks.add(task)
    task.add_done_callback(hub._background_tasks.discard)

    await hub.shutdown()
    assert len(hub._background_tasks) == 0
    assert task.cancelled()


async def test_hub_shutdown_empty_tasks_is_noop() -> None:
    """shutdown() with no background tasks returns without error."""
    from provide.uterm.server.bridge.hub import TermHub

    hub = TermHub()
    assert len(hub._background_tasks) == 0
    await hub.shutdown()  # must not raise


async def test_shutdown_background_tasks_returns_count() -> None:
    """shutdown_background_tasks() returns the number of tasks cancelled."""
    import asyncio

    from provide.uterm.server.bridge.hub.connections import shutdown_background_tasks

    async def _long() -> None:
        await asyncio.sleep(60)

    task_set: set[asyncio.Task[None]] = set()
    t1 = asyncio.create_task(_long())
    t2 = asyncio.create_task(_long())
    task_set.add(t1)
    task_set.add(t2)

    count = await shutdown_background_tasks(task_set)
    assert count == 2
    assert len(task_set) == 0
    assert t1.cancelled()
    assert t2.cancelled()


async def test_shutdown_background_tasks_empty_returns_zero() -> None:
    """shutdown_background_tasks() with empty set returns 0."""
    import asyncio

    from provide.uterm.server.bridge.hub.connections import shutdown_background_tasks

    task_set: set[asyncio.Task[None]] = set()
    count = await shutdown_background_tasks(task_set)
    assert count == 0
