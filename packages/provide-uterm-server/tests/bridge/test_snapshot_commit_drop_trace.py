#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A snapshot the hub refuses to commit must not vanish without a word.

``commit_snapshot_event`` drops a frame whose sending websocket is no longer the
registered worker: it returns ``None``, stores nothing, and the dispatcher then
skips the broadcast. That is the correct outcome — a frame from a superseded
connection is stale by definition — but it was completely silent, and silence
here is indistinguishable from the two failures it sits between.

Live 2026-08-14, four wedges: the worker ingested the bytes, rendered the new
screen and published it (its own trace shows the arm, the capture and a new
screen hash), while the polling client kept reading a snapshot three chunks old
until it timed out. From the hub's logs there was no way to tell whether that
published frame was stored, dropped here, or never arrived — the three need
different fixes and none of them left a trace.

So the drop gets a log line. These assertions are written against the log
itself, because a drop nobody can observe from a live run is the bug.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from provide.uterm.server.bridge.hub import TermHub

_WORKER_ID = "bot1"


def _snapshot(*, screen: str = "current") -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 1, "y": 0},
        "cols": 132,
        "rows": 43,
        "screen_hash": f"sha256:{screen}",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": None,
        "raw_tail": screen,
        "ts": 1234.5,
    }


async def _make_hub() -> tuple[TermHub, AsyncMock]:
    hub = TermHub()
    worker = AsyncMock()
    await hub.register_worker(_WORKER_ID, worker)
    return hub, worker


@pytest.mark.asyncio
async def test_a_superseded_worker_frame_is_dropped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The frame is still refused — the point is that it now says so."""
    hub, _worker = await _make_hub()
    superseded = AsyncMock()  # never registered: stands in for a replaced connection

    with caplog.at_level(logging.WARNING):
        committed = await hub.commit_snapshot_event(_WORKER_ID, _snapshot(), expected_worker=superseded)

    assert committed is None, "a frame from a superseded connection must not commit"
    assert "snapshot_commit_dropped" in caplog.text, f"the drop must be traced; got {caplog.text!r}"
    assert _WORKER_ID in caplog.text


@pytest.mark.asyncio
async def test_an_unknown_worker_frame_is_dropped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    """No registry entry at all is the same class of drop and needs the same trace."""
    hub = TermHub()
    stranger = AsyncMock()

    with caplog.at_level(logging.WARNING):
        committed = await hub.commit_snapshot_event("ghost", _snapshot(), expected_worker=stranger)

    assert committed is None
    assert "snapshot_commit_dropped" in caplog.text


@pytest.mark.asyncio
async def test_the_owning_worker_commits_without_a_drop_line(caplog: pytest.LogCaptureFixture) -> None:
    """The happy path must stay quiet, or the trace is noise nobody can read."""
    hub, worker = await _make_hub()

    with caplog.at_level(logging.WARNING):
        committed = await hub.commit_snapshot_event(_WORKER_ID, _snapshot(), expected_worker=worker)

    assert committed is not None
    assert committed["screen"] == "current"
    assert "snapshot_commit_dropped" not in caplog.text


@pytest.mark.asyncio
async def test_an_unfenced_commit_still_stores_and_stays_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """``expected_worker=None`` is the unfenced path and must not log a drop."""
    hub, _worker = await _make_hub()

    with caplog.at_level(logging.WARNING):
        committed = await hub.commit_snapshot_event(_WORKER_ID, _snapshot(screen="unfenced"))

    assert committed is not None
    stored = await hub.get_last_snapshot(_WORKER_ID)
    assert stored is not None and stored["screen"] == "unfenced"
    assert "snapshot_commit_dropped" not in caplog.text
