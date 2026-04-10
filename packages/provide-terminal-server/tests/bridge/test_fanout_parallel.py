#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for FanOutController — parallel send and group CRUD."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

from provide.terminal.bridge.fanout._controller import FanOutController
from provide.terminal.bridge.fanout._models import FanOutGroup
from provide.terminal.bridge.hub import EventBus, TermHub


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
    result = await ctrl.send("g1", "ls\n", principal="admin")
    await task

    assert result.group_id == "g1"
    assert result.command == "ls\n"
    assert len(result.results) == 3
    assert all(r.ok for r in result.results)
    assert result.failed_sessions == []


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
        raise AssertionError("Expected ValueError")  # noqa: TRY301
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
