#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression coverage for atomic snapshot/event correlation."""

from __future__ import annotations

import pytest

from provide.uterm.server.bridge.hub.event_bus import EventBus
from provide.uterm.server.bridge.models import WorkerTermState

from .test_routes_advanced import make_app


@pytest.mark.asyncio
async def test_snapshot_commit_correlates_stored_snapshot_and_event() -> None:
    _app, hub = make_app()
    worker_id = "bot1"
    hub.registry._workers[worker_id] = WorkerTermState()

    term = await hub.append_event(worker_id, "term", {"data": "P"})
    event_bus = EventBus()
    hub.event_bus = event_bus
    snapshot = {
        "type": "snapshot",
        "screen": "P",
        "cursor": {"x": 1, "y": 0},
        "cols": 132,
        "rows": 43,
        "screen_hash": "sha256:screen",
        "prompt_detected": {"id": "command", "confidence": 0.98},
        "raw_tail": "P",
        "ts": 1234.5,
    }

    async with event_bus.watch(worker_id, event_types=["snapshot"]) as subscription:
        committed = await hub.commit_snapshot_event(worker_id, snapshot)
        bus_event = subscription.queue.get_nowait()

    assert committed == {**snapshot, "event_seq": term["seq"] + 1}
    assert "event_seq" not in snapshot

    stored = await hub.get_last_snapshot(worker_id)
    assert stored == committed

    last_event = hub.registry._workers[worker_id].events[-1]
    assert last_event["seq"] == committed["event_seq"]
    assert last_event["type"] == "snapshot"
    assert last_event["data"] == committed
    assert last_event["data"]["cursor"] == snapshot["cursor"]
    assert bus_event == {"worker_id": worker_id, **last_event}

    missing = await hub.commit_snapshot_event("missing", snapshot)
    assert missing == {**snapshot, "event_seq": 0}
    assert hub.registry.get("missing") is None
