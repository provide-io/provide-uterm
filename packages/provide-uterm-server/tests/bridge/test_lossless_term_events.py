#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Large terminal frames must remain complete in the bounded event ring."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState
from provide.uterm.server.bridge.worker_link import TermBridge


class _Session:
    def add_watch(self, callback: Any, *, interval_s: float) -> None:
        self.watch = callback


@pytest.mark.parametrize("size", [0, 8191, 8192, 8193, 3 * 8192 + 7])
async def test_default_bridge_retains_every_character_in_default_ring(size: int) -> None:
    session = _Session()
    bridge = TermBridge(SimpleNamespace(session=session), "offline", "http://unused.invalid", encoding="utf-8")
    bridge.attach_session()
    text = ("\x1b[31m█\x1b[0m" * (size + 1))[:size]
    try:
        session.watch({}, text.encode("utf-8"))
    finally:
        await bridge.stop()
    frames = [bridge._send_q.get_nowait() for _ in range(bridge._send_q.qsize())]
    assert all(frame["type"] == "term" for frame in frames)
    assert "".join(frame["data"] for frame in frames) == text
    hub = TermHub()
    hub.registry._workers["offline"] = WorkerTermState()
    for frame in frames:
        await hub.append_event("offline", "term", {"data": frame["data"]})
    ring = hub.registry._workers["offline"].events
    assert "".join(event["data"]["data"] for event in ring) == text
    assert all(len(frame["data"]) <= hub.max_event_data_chars for frame in frames)


@pytest.mark.parametrize("cap", [256, 1024])
async def test_bridge_can_match_a_smaller_configured_ring(cap: int) -> None:
    session = _Session()
    bridge = TermBridge(
        SimpleNamespace(session=session),
        "offline",
        "http://unused.invalid",
        max_event_data_chars=cap,
    )
    bridge.attach_session()
    text = "x" * (3 * cap + 1)
    try:
        session.watch({}, text.encode("ascii"))
    finally:
        await bridge.stop()
    frames = [bridge._send_q.get_nowait() for _ in range(bridge._send_q.qsize())]
    assert [len(frame["data"]) for frame in frames] == [cap, cap, cap, 1]
    hub = TermHub(max_event_data_chars=cap)
    hub.registry._workers["offline"] = WorkerTermState()
    for frame in frames:
        await hub.append_event("offline", "term", {"data": frame["data"]})
    assert "".join(event["data"]["data"] for event in hub.registry._workers["offline"].events) == text
