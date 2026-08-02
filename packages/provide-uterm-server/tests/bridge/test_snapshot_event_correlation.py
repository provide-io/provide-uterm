#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression coverage for atomic snapshot/event correlation."""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest

from provide.uterm.bridge.schemas import SnapshotFrame
from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder
from provide.uterm.server.bridge.fanout._collector import OutputCollector
from provide.uterm.server.bridge.hub import EventBus, TermHub
from provide.uterm.server.bridge.models import WorkerTermState
from provide.uterm.server.bridge.routes.websockets_worker import _dispatch_worker_frame

from .test_routes_advanced import make_app

_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret


def _snapshot(*, screen: str = "P") -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 1, "y": 0},
        "cols": 132,
        "rows": 43,
        "screen_hash": "sha256:raw-screen",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": "command", "matched": "P"},
        "raw_tail": screen,
        "ts": 1234.5,
    }


def _snapshot_frames(websocket: AsyncMock) -> list[dict[str, Any]]:
    decoder = ControlFrameDecoder()
    frames: list[dict[str, Any]] = []
    for call in websocket.send_text.call_args_list:
        for event in decoder.feed(call.args[0]):
            if isinstance(event, ControlChunk) and event.control.get("type") == "snapshot":
                frames.append(dict(event.control))
    return frames


@pytest.mark.asyncio
async def test_dispatch_broadcasts_raw_correlated_snapshot_and_redacts_ring_event() -> None:
    _app, hub = make_app()
    worker_id = "bot1"
    browser = AsyncMock()
    hub.registry._workers[worker_id] = WorkerTermState(browsers={browser: "viewer"})

    term = await hub.append_event(worker_id, "term", {"data": "P"})
    snapshot = _snapshot(screen=f"token {_AWS_KEY}")
    await _dispatch_worker_frame(hub, worker_id, "snapshot", snapshot)

    wire = _snapshot_frames(browser)[-1]
    stored = await hub.get_last_snapshot(worker_id)
    recent = await hub.get_recent_events(worker_id, limit=10)
    ring = recent[-1]

    assert wire == stored
    assert wire["event_seq"] == term["seq"] + 1
    assert wire["screen"] == snapshot["screen"]
    assert wire["raw_tail"] == snapshot["raw_tail"]
    assert wire["screen_hash"] == snapshot["screen_hash"]
    SnapshotFrame.model_validate(wire)

    assert ring["seq"] == wire["event_seq"]
    assert ring["type"] == "snapshot"
    assert ring["data"]["event_seq"] == wire["event_seq"]
    assert ring["data"]["prompt_id"] == "command"
    assert _AWS_KEY not in ring["data"]["screen"]
    assert ring["data"]["screen"] == "token [AWS_ACCESS_KEY_REDACTED]"


@pytest.mark.asyncio
async def test_snapshot_commit_owns_input_return_bus_and_reader_copies() -> None:
    event_bus = EventBus()
    hub = TermHub(event_bus=event_bus)
    worker_id = "bot1"
    hub.registry._workers[worker_id] = WorkerTermState()
    snapshot = _snapshot()
    original = deepcopy(snapshot)

    async with event_bus.watch(worker_id, event_types=["snapshot"]) as subscription:
        returned = await hub.commit_snapshot_event(worker_id, snapshot)
        bus_event = subscription.queue.get_nowait()

    stored_read = await hub.get_last_snapshot(worker_id)
    recent_read = await hub.get_recent_events(worker_id, limit=10)
    state = hub.registry._workers[worker_id]
    assert bus_event is not None
    assert stored_read is not None
    assert stored_read is not state.last_snapshot
    assert recent_read[-1] is not state.events[-1]
    assert returned is not state.last_snapshot
    assert bus_event["data"] is not state.events[-1]["data"]
    assert state.last_snapshot is not state.events[-1]["data"]

    snapshot["cursor"]["x"] = 99
    snapshot["prompt_detected"]["matched"] = "input-mutated"
    returned["cursor"]["x"] = 98
    returned["prompt_detected"]["matched"] = "return-mutated"
    bus_event["data"]["cursor"]["x"] = 97
    stored_read["cursor"]["x"] = 96
    recent_read[-1]["data"]["cursor"]["x"] = 95

    assert state.last_snapshot == {**original, "event_seq": 1}
    assert state.events[-1]["data"]["cursor"] == original["cursor"]
    assert state.events[-1]["data"]["prompt_detected"] == original["prompt_detected"]
    fresh_snapshot = await hub.get_last_snapshot(worker_id)
    assert fresh_snapshot is not None
    assert fresh_snapshot["cursor"] == original["cursor"]
    assert (await hub.get_recent_events(worker_id, limit=10))[-1]["data"]["cursor"] == original["cursor"]


@pytest.mark.asyncio
async def test_supervised_operation_gets_raw_snapshot_while_public_event_stays_redacted() -> None:
    event_bus = EventBus()
    hub = TermHub(event_bus=event_bus)
    worker_id = "bot1"
    hub.registry._workers[worker_id] = WorkerTermState()
    screen = f"ordinary gameplay token {_AWS_KEY}"
    capture = await OutputCollector().open(hub, worker_id)

    try:
        async with event_bus.watch(worker_id, event_types=["snapshot"]) as public_subscription:
            await hub.commit_snapshot_event(worker_id, _snapshot(screen=screen))
            public_event = public_subscription.queue.get_nowait()
        operation_screen, _elapsed_ms = await capture.collect(quiesce_ms=1, max_ms=50)
    finally:
        await capture.close()

    assert operation_screen == screen
    assert public_event is not None
    assert public_event["data"]["screen"] == "ordinary gameplay token [AWS_ACCESS_KEY_REDACTED]"
    assert _AWS_KEY not in public_event["data"]["screen"]


@pytest.mark.asyncio
async def test_concurrent_snapshot_commits_and_appends_share_one_sequence() -> None:
    _app, hub = make_app()
    worker_id = "bot1"
    hub.registry._workers[worker_id] = WorkerTermState()

    operations = []
    for index in range(20):
        operations.append(hub.append_event(worker_id, "term", {"data": str(index)}))
        operations.append(hub.commit_snapshot_event(worker_id, _snapshot(screen=str(index))))
    await asyncio.gather(*operations)

    state = hub.registry._workers[worker_id]
    assert [event["seq"] for event in state.events] == list(range(1, 41))
    for event in state.events:
        if event["type"] == "snapshot":
            assert event["data"]["event_seq"] == event["seq"]


@pytest.mark.asyncio
async def test_snapshot_commit_updates_min_sequence_after_bounded_overflow() -> None:
    _app, hub = make_app()
    worker_id = "bot1"
    hub.registry._workers[worker_id] = WorkerTermState(events=deque(maxlen=2))

    await hub.append_event(worker_id, "term", {"data": "before"})
    await hub.commit_snapshot_event(worker_id, _snapshot(screen="one"))
    await hub.commit_snapshot_event(worker_id, _snapshot(screen="two"))

    state = hub.registry._workers[worker_id]
    assert [event["seq"] for event in state.events] == [2, 3]
    assert state.event_seq == 3
    assert state.min_event_seq == 2


@pytest.mark.asyncio
async def test_snapshot_commit_for_unknown_worker_returns_owned_raw_sequence_zero() -> None:
    _app, hub = make_app()
    snapshot = _snapshot(screen=f"token {_AWS_KEY}")
    original = deepcopy(snapshot)

    returned = await hub.commit_snapshot_event("missing", snapshot)
    returned["cursor"]["x"] = 99

    assert returned["screen"] == original["screen"]
    assert returned["event_seq"] == 0
    assert snapshot == original
    assert hub.registry.get("missing") is None
