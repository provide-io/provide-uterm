#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for FanOutController — parallel send and group CRUD."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server.bridge.fanout._controller import FanOutController
from provide.uterm.server.bridge.fanout._models import FanOutGroup
from provide.uterm.server.bridge.hub import EventBus, TermHub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_hub_with_workers(*worker_ids: str) -> TermHub:
    """Create a TermHub with EventBus and register workers."""
    hub = TermHub(event_bus=EventBus())
    for wid in worker_ids:
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        await hub.register_worker(wid, ws)
    return hub


def _make_group(worker_ids: list[str], **kwargs: object) -> FanOutGroup:
    return FanOutGroup(
        group_id="g1",
        name="test-group",
        worker_ids=worker_ids,
        created_by="admin",
        created_at=time.time(),
        quiesce_ms=300,
        max_response_ms=5_000,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Parallel send — all connected
# ---------------------------------------------------------------------------


async def test_parallel_send_all_connected() -> None:
    """Three workers all receive input, results are aggregated."""
    hub = await _make_hub_with_workers("w1", "w2", "w3")
    ctrl = FanOutController(hub)
    group = _make_group(["w1", "w2", "w3"])
    await ctrl.create_group(group, principal="admin")

    async def _emit_output() -> None:
        await asyncio.sleep(0.02)
        for wid in ("w1", "w2", "w3"):
            await hub.append_event(wid, "term", {"data": "ok\n"})

    task = asyncio.create_task(_emit_output())
    result = await ctrl._send_parallel(group, "ls\n", 300, 5_000, principal="admin")
    await task

    assert result.group_id == "g1"
    assert result.command == "ls\n"
    assert len(result.results) == 3
    assert all(r.ok for r in result.results)
    assert result.failed_sessions == []


async def test_parallel_captures_output_emitted_inside_send() -> None:
    hub = await _make_hub_with_workers("w1", "w2")
    ctrl = FanOutController(hub)
    group = _make_group(["w1", "w2"])

    async def _send_with_immediate_output(worker_id: str, frame: dict[str, object]) -> bool:
        await hub.append_event(worker_id, "term", {"data": f"immediate-{worker_id}"})
        return True

    hub.send_worker = _send_with_immediate_output  # type: ignore[assignment]

    result = await ctrl._send_parallel(group, "id\n", 10, 100, principal="admin")

    assert [item.output_delta for item in result.results] == ["immediate-w1", "immediate-w2"]


async def test_parallel_capture_open_failure_blocks_member_input(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.send_worker = AsyncMock(return_value=False)
    group = _make_group(["w1", "w2"])

    class Handle:
        async def collect(self, **kwargs: object) -> tuple[str, int]:
            return "", 0

        async def close(self) -> None:
            return None

    class Collector:
        async def open(self, hub: object, worker_id: str) -> Handle:
            if worker_id == "w1":
                raise RuntimeError("subscription limit")
            return Handle()

    monkeypatch.setattr("provide.uterm.server.bridge.fanout._controller.OutputCollector", Collector)
    result = await FanOutController(hub)._send_parallel(group, "id\n", 10, 100, principal="admin")

    assert hub.send_worker.await_args_list[0].args[0] == "w2"
    assert all(call.args[0] != "w1" for call in hub.send_worker.await_args_list)
    assert result.failed_sessions == ["w1", "w2"]


async def test_parallel_rejected_send_closes_all_prepared_captures_once(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.send_worker = AsyncMock(return_value=False)
    group = _make_group(["w1", "w2"])
    handles: dict[str, MagicMock] = {}

    class Collector:
        async def open(self, hub: object, worker_id: str) -> MagicMock:
            handle = MagicMock()
            handle.close = AsyncMock()
            handle.collect = AsyncMock(return_value=("", 0))
            handles[worker_id] = handle
            return handle

    monkeypatch.setattr("provide.uterm.server.bridge.fanout._controller.OutputCollector", Collector)
    await FanOutController(hub)._send_parallel(group, "id\n", 10, 100, principal="admin")

    assert set(handles) == {"w1", "w2"}
    for handle in handles.values():
        handle.close.assert_awaited_once()


async def test_parallel_cancellation_closes_all_prepared_captures_once(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.send_worker = AsyncMock(side_effect=asyncio.CancelledError)
    group = _make_group(["w1", "w2"])
    handles: dict[str, MagicMock] = {}

    class Collector:
        async def open(self, hub: object, worker_id: str) -> MagicMock:
            handle = MagicMock()
            handle.close = AsyncMock()
            handle.collect = AsyncMock(return_value=("", 0))
            handles[worker_id] = handle
            return handle

    monkeypatch.setattr("provide.uterm.server.bridge.fanout._controller.OutputCollector", Collector)
    with pytest.raises(asyncio.CancelledError):
        await FanOutController(hub)._send_parallel(group, "id\n", 10, 100, principal="admin")

    for handle in handles.values():
        handle.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Group not found → empty result
# ---------------------------------------------------------------------------


async def test_parallel_send_group_not_found() -> None:
    """Sending to a non-existent group returns an empty result."""
    hub = TermHub(event_bus=EventBus())
    ctrl = FanOutController(hub)

    result = await ctrl.send("nonexistent", "ls\n", principal="admin")

    assert result.group_id == "nonexistent"
    assert result.results == []
    assert result.failed_sessions == []


# ---------------------------------------------------------------------------
# Max group size enforcement
# ---------------------------------------------------------------------------


async def test_create_group_enforces_max_size() -> None:
    """Creating a group with too many workers raises ValueError."""
    hub = TermHub(event_bus=EventBus())
    ctrl = FanOutController(hub, max_group_size=2)
    group = _make_group(["w1", "w2", "w3"])

    try:
        await ctrl.create_group(group, principal="admin")
        raise AssertionError("Expected ValueError")
    except ValueError as exc:
        assert "exceeds max" in str(exc)


# ---------------------------------------------------------------------------
# CRUD round-trip: create + list
# ---------------------------------------------------------------------------


async def test_create_group_and_list() -> None:
    """Create a group, then list it back."""
    hub = TermHub(event_bus=EventBus())
    ctrl = FanOutController(hub)
    group = _make_group(["w1"])
    gid = await ctrl.create_group(group, principal="admin")

    assert gid == "g1"
    groups = await ctrl.list_groups("admin")
    assert len(groups) == 1
    assert groups[0].group_id == "g1"


# ---------------------------------------------------------------------------
# Delete group
# ---------------------------------------------------------------------------


async def test_delete_group() -> None:
    """Group is gone after deletion."""
    hub = TermHub(event_bus=EventBus())
    ctrl = FanOutController(hub)
    group = _make_group(["w1"])
    await ctrl.create_group(group, principal="admin")

    await ctrl.delete_group("g1", principal="admin")
    assert await ctrl.get_group("g1", principal="admin") is None
    assert await ctrl.list_groups("admin") == []


# ---------------------------------------------------------------------------
# Grant access
# ---------------------------------------------------------------------------


async def test_grant_access() -> None:
    """Grantee can see the group in list_groups after being granted."""
    hub = TermHub(event_bus=EventBus())
    ctrl = FanOutController(hub)
    group = _make_group(["w1"])
    await ctrl.create_group(group, principal="admin")

    # Before grant
    assert await ctrl.list_groups("bob") == []

    # Grant
    await ctrl.grant_access("g1", "bob", principal="admin")

    # After grant
    groups = await ctrl.list_groups("bob")
    assert len(groups) == 1
    assert groups[0].group_id == "g1"
