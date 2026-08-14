#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A snapshot already stored must not be withheld for being "too old".

``wait_for_snapshot`` gated on ``snap.ts > req_ts`` — strictly newer than the
REQUEST. That was defensible while snapshots existed only in reply to a
``snapshot_req``: anything already stored was, by construction, from a previous
question. Once workers began PUSHING on screen change it became wrong, and
wrong in the worst direction: the push lands the fresh screen microseconds
BEFORE the poller asks, so the very frame the caller needs is the one the gate
discards. The caller then waits out the full window, gets ``None``, and falls
back to its own older cached screen.

Measured 2026-08-14, six wedges. The last one, at one-second resolution:

    10:20:21  hash=f883f07765b0 chunks=51   <- new screen published, once
    10:20:21  snapshot_push_done seq=51 captured=51 rearm=False
              ... 23 seconds of no frames at all ...
    10:20:44  hash=f883f07765b0 chunks=51   <- the same frame, finally re-served

The client timed out four seconds into that gap still reporting ``chunks=48``,
and the hub logged 229 ``snapshot_wait_timeout``. The frame was correct, stored
and never handed over.

Freshness is therefore expressed against what the CALLER has already been
served — a monotonic ``event_seq`` — not against when it happened to ask.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from provide.uterm.server.bridge.hub import TermHub

_WORKER_ID = "bot1"


def _snapshot(*, screen: str, ts: float) -> dict[str, Any]:
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
        "ts": ts,
    }


async def _hub_with_stored(screen: str, *, ts: float) -> tuple[TermHub, dict[str, Any]]:
    hub = TermHub()
    worker = AsyncMock()
    await hub.register_worker(_WORKER_ID, worker)
    committed = await hub.commit_snapshot_event(_WORKER_ID, _snapshot(screen=screen, ts=ts))
    return hub, committed


@pytest.mark.asyncio
async def test_a_frame_pushed_just_before_the_poll_is_returned() -> None:
    """The regression: stored microseconds ago, older than req_ts, still wanted."""
    hub, committed = await _hub_with_stored("shipyards", ts=time.time() - 0.05)

    got = await hub.wait_for_snapshot(_WORKER_ID, timeout_ms=200, after_event_seq=0)

    assert got is not None, "the pushed frame was withheld for predating the request"
    assert got["screen"] == "shipyards"
    assert got["event_seq"] == committed["event_seq"]


@pytest.mark.asyncio
async def test_a_frame_already_served_is_not_returned_again() -> None:
    """The gate still has to WAIT — otherwise it returns stale state forever."""
    hub, committed = await _hub_with_stored("old", ts=time.time() - 0.05)

    got = await hub.wait_for_snapshot(_WORKER_ID, timeout_ms=120, after_event_seq=int(committed["event_seq"]))

    assert got is None, "a snapshot this caller already holds must not count as fresh"


@pytest.mark.asyncio
async def test_a_frame_arriving_during_the_wait_is_returned() -> None:
    """The pre-existing behaviour: something newer shows up mid-wait."""
    hub, committed = await _hub_with_stored("old", ts=time.time() - 0.05)
    seen = int(committed["event_seq"])

    async def _late() -> None:
        await hub.commit_snapshot_event(_WORKER_ID, _snapshot(screen="new", ts=time.time()))

    import asyncio

    task = asyncio.create_task(_late())
    got = await hub.wait_for_snapshot(_WORKER_ID, timeout_ms=1500, after_event_seq=seen)
    await task

    assert got is not None
    assert got["screen"] == "new"


@pytest.mark.asyncio
async def test_the_wall_clock_gate_still_applies_without_a_cursor() -> None:
    """Callers that pass no cursor keep the previous semantics exactly."""
    hub, _committed = await _hub_with_stored("old", ts=time.time() - 5.0)

    got = await hub.wait_for_snapshot(_WORKER_ID, timeout_ms=120)

    assert got is None, "without a cursor a stale stored frame must still not satisfy the wait"


@pytest.mark.asyncio
async def test_an_unknown_worker_returns_none() -> None:
    hub = TermHub()

    assert await hub.wait_for_snapshot("ghost", timeout_ms=100, after_event_seq=0) is None
