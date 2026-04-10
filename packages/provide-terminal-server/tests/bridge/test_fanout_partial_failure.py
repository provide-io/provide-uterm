#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for FanOutController — partial failure scenarios."""

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
# Partial failure — disconnected workers
# ---------------------------------------------------------------------------


async def test_partial_failure_disconnected_workers() -> None:
    """Two of three workers are disconnected; failed_sessions is populated."""
    hub = TermHub(event_bus=EventBus())
    # Only register w1 — w2 and w3 are "disconnected"
    ws = AsyncMock()
    ws.send_text = AsyncMock()
    await hub.register_worker("w1", ws)

    ctrl = FanOutController(hub)
    group = _make_group(["w1", "w2", "w3"])
    await ctrl.create_group(group, principal="admin")

    async def _emit_output() -> None:
        await asyncio.sleep(0.02)
        await hub.append_event("w1", "term", {"data": "output from w1"})

    task = asyncio.create_task(_emit_output())
    result = await ctrl.send("g1", "cmd\n", principal="admin")
    await task

    assert len(result.results) == 3

    # w1 succeeded
    assert result.results[0].ok is True
    assert result.results[0].worker_id == "w1"
    assert result.results[0].output_delta == "output from w1"

    # w2, w3 failed
    assert result.results[1].ok is False
    assert result.results[2].ok is False
    assert set(result.failed_sessions) == {"w2", "w3"}
