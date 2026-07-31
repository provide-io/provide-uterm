#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for FanOutController — sequential send mode."""

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
        mode="sequential",
        quiesce_ms=300,
        max_response_ms=5_000,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Sequential — workers processed in order
# ---------------------------------------------------------------------------


async def test_sequential_iterates_in_order() -> None:
    """Workers are processed w1 -> w2 -> w3 in order."""
    hub = await _make_hub_with_workers("w1", "w2", "w3")
    ctrl = FanOutController(hub)
    group = _make_group(["w1", "w2", "w3"])
    await ctrl.create_group(group, principal="admin")

    order: list[str] = []

    # Patch send_worker to track order and schedule delayed output
    _orig_send = hub.send_worker
    _bg_tasks: list[asyncio.Task[None]] = []

    async def _delayed_emit(wid: str) -> None:
        await asyncio.sleep(0.05)
        await hub.append_event(wid, "term", {"data": f"output-{wid}"})

    async def _tracking_send(wid: str, msg: dict) -> bool:  # type: ignore[type-arg]
        order.append(wid)
        result = await _orig_send(wid, msg)
        _bg_tasks.append(asyncio.create_task(_delayed_emit(wid)))
        return result

    hub.send_worker = _tracking_send  # type: ignore[assignment]

    result = await ctrl._send_sequential(group, "cmd\n", 300, 5_000, principal="admin")
    for t in _bg_tasks:
        await t

    assert order == ["w1", "w2", "w3"]
    assert len(result.results) == 3
    assert all(r.ok for r in result.results)
    assert result.results[0].output_delta == "output-w1"
    assert result.results[1].output_delta == "output-w2"
    assert result.results[2].output_delta == "output-w3"


async def test_sequential_captures_output_emitted_inside_send() -> None:
    hub = await _make_hub_with_workers("w1", "w2")
    ctrl = FanOutController(hub)
    group = _make_group(["w1", "w2"])

    async def _send_with_immediate_output(worker_id: str, frame: dict[str, object]) -> bool:
        await hub.append_event(worker_id, "term", {"data": f"immediate-{worker_id}"})
        return True

    hub.send_worker = _send_with_immediate_output  # type: ignore[assignment]

    result = await ctrl._send_sequential(group, "id\n", 10, 100, principal="admin")

    assert [item.output_delta for item in result.results] == ["immediate-w1", "immediate-w2"]


async def test_sequential_rejected_send_closes_capture_once(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.send_worker = AsyncMock(return_value=False)
    handle = MagicMock()
    handle.close = AsyncMock()
    handle.collect = AsyncMock(return_value=("", 0))

    class Collector:
        async def open(self, hub: object, worker_id: str) -> MagicMock:
            return handle

    monkeypatch.setattr("provide.uterm.server.bridge.fanout._controller.OutputCollector", Collector)
    result = await FanOutController(hub)._send_sequential(_make_group(["w1"]), "id\n", 10, 100, principal="admin")

    assert result.failed_sessions == ["w1"]
    handle.collect.assert_not_awaited()
    handle.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Sequential — stop on first error
# ---------------------------------------------------------------------------


async def test_sequential_stop_on_first_error() -> None:
    """Error regex match on w2 stops processing; w3 is marked failed."""
    hub = await _make_hub_with_workers("w1", "w2", "w3")
    ctrl = FanOutController(hub)
    group = _make_group(
        ["w1", "w2", "w3"],
        stop_on_first_error=True,
        error_pattern=r"ERROR:",
    )
    await ctrl.create_group(group, principal="admin")

    _orig_send = hub.send_worker
    _bg_tasks: list[asyncio.Task[None]] = []

    async def _delayed_emit(wid: str) -> None:
        await asyncio.sleep(0.05)
        if wid == "w2":
            await hub.append_event(wid, "term", {"data": "ERROR: something failed"})
        else:
            await hub.append_event(wid, "term", {"data": "success"})

    async def _send_with_output(wid: str, msg: dict) -> bool:  # type: ignore[type-arg]
        result = await _orig_send(wid, msg)
        _bg_tasks.append(asyncio.create_task(_delayed_emit(wid)))
        return result

    hub.send_worker = _send_with_output  # type: ignore[assignment]

    result = await ctrl._send_sequential(group, "deploy\n", 300, 5_000, principal="admin")
    for t in _bg_tasks:
        await t

    # w1 succeeded
    assert result.results[0].ok is True
    assert result.results[0].worker_id == "w1"

    # w2 succeeded (error was in output, but send itself worked)
    assert result.results[1].ok is True
    assert result.results[1].worker_id == "w2"
    assert "ERROR:" in (result.results[1].output_delta or "")

    # w3 was skipped/failed because processing stopped after w2's error
    assert result.results[2].ok is False
    assert result.results[2].worker_id == "w3"
    assert "w3" in result.failed_sessions
