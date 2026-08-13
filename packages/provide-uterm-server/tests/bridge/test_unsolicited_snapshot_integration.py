#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""An unsolicited snapshot must traverse the hub exactly like a requested one.

TermBridge now pushes a snapshot when the screen changes, not only in reply to a
``snapshot_req``. That is new traffic on this wire: before it, every inbound
snapshot was the answer to a request the hub had just sent. The unit tests cover
the bridge's side of that; these cover the hub's, because "the hub does not
correlate snapshots to requests" is a claim about code nobody had executed with
an uncorrelated snapshot.

Two things are asserted, and the second is a behaviour change worth knowing:

  1. A pushed snapshot commits and broadcasts identically to a requested one.
  2. A pushed snapshot SATISFIES a pending wait_for_snapshot. That is the point
     of the feature — a waiter no longer sits out the full poll interval — but
     it also means the snapshot_wait_timeout diagnostic added in 2163d535 stops
     firing for a request path that is broken, because a push arriving on time
     is indistinguishable to the waiter from a reply. See the comment at that
     warning.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState
from provide.uterm.server.bridge.routes.websockets_worker import _dispatch_worker_frame


def _snapshot(*, screen: str = "sector 1", ts: float | None = None) -> dict[str, Any]:
    """A snapshot frame shaped exactly as TermBridge._send_snapshot emits one."""
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 1, "y": 2},
        "cols": 80,
        "rows": 25,
        "screen_hash": f"hash-{screen}",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": None,
        "raw_tail": screen,
        "ts": time.time() if ts is None else ts,
    }


async def _register(hub: TermHub, worker_id: str = "w1") -> WorkerTermState:
    state = WorkerTermState()
    hub.registry._workers[worker_id] = state
    return state


class TestUnsolicitedSnapshotThroughTheHub:
    async def test_a_pushed_snapshot_commits_and_broadcasts(self) -> None:
        """No request preceded it; the hub must not care."""
        hub = TermHub()
        await _register(hub)
        hub.broadcast = AsyncMock()  # type: ignore[method-assign]

        await _dispatch_worker_frame(hub, "w1", "snapshot", _snapshot())

        hub.broadcast.assert_awaited_once()
        committed = hub.broadcast.await_args.args[1]
        assert committed["type"] == "snapshot"
        assert committed["screen_hash"] == "hash-sector 1"
        # Committed to the registry, so a later poller sees it as current state.
        async with hub._lock:
            assert hub.registry.get("w1").last_snapshot["screen_hash"] == "hash-sector 1"

    async def test_a_pushed_snapshot_satisfies_a_pending_waiter(self) -> None:
        """The behaviour change: a waiter is released by a push, not just a reply.

        request_snapshot is stubbed to do NOTHING — modelling a request path that
        never reaches the worker. Pre-push this wait could only time out; now the
        push alone releases it.
        """
        hub = TermHub()
        await _register(hub)
        hub.request_snapshot = AsyncMock()  # type: ignore[method-assign]
        hub.broadcast = AsyncMock()  # type: ignore[method-assign]

        waiter = asyncio.create_task(hub.wait_for_snapshot("w1", timeout_ms=3000))
        await asyncio.sleep(0)  # let the waiter take its req_ts before we push

        await _dispatch_worker_frame(hub, "w1", "snapshot", _snapshot(screen="sector 2"))

        got = await asyncio.wait_for(waiter, timeout=5.0)
        assert got is not None, "the pushed snapshot did not release the waiter"
        assert got["screen_hash"] == "hash-sector 2"
        # The request never went anywhere, yet the waiter succeeded — this is
        # exactly the masking the module docstring warns about.
        hub.request_snapshot.assert_awaited_once()

    async def test_a_stale_pushed_snapshot_does_not_satisfy_a_waiter(self) -> None:
        """Freshness is still enforced: ts must beat the request, push or not."""
        hub = TermHub()
        await _register(hub)
        hub.request_snapshot = AsyncMock()  # type: ignore[method-assign]
        hub.broadcast = AsyncMock()  # type: ignore[method-assign]

        # A snapshot older than any request that could follow it.
        await _dispatch_worker_frame(hub, "w1", "snapshot", _snapshot(ts=time.time() - 60))

        got = await hub.wait_for_snapshot("w1", timeout_ms=150)
        assert got is None
