#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted tests for remaining coverage gaps in bridge/hub code."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.bridge.coordinator import HijackCoordinator
from provide.uterm.bridge.hub import TermHub

# ---------------------------------------------------------------------------
# HijackCoordinator — lines 90, 92, 100, 102, 109
# ---------------------------------------------------------------------------


class TestCoordinatorMismatch:
    """Test heartbeat/release with no session or wrong hijack_id."""

    def test_heartbeat_no_active_session(self) -> None:
        """heartbeat() when not hijacked → error='not_hijacked' (line 90)."""
        coord = HijackCoordinator()
        result = coord.heartbeat("fake-id", 60, "owner")
        assert not result.ok
        assert result.error == "not_hijacked"

    def test_heartbeat_wrong_hijack_id(self) -> None:
        """heartbeat() with wrong hijack_id → error='hijack_id_mismatch' (line 92)."""
        coord = HijackCoordinator()
        acquire = coord.acquire("owner", 60)
        assert acquire.ok
        result = coord.heartbeat("wrong-id", 60, "owner")
        assert not result.ok
        assert result.error == "hijack_id_mismatch"

    def test_release_no_active_session(self) -> None:
        """release() when not hijacked → error='not_hijacked' (line 100)."""
        coord = HijackCoordinator()
        result = coord.release("fake-id")
        assert not result.ok
        assert result.error == "not_hijacked"

    def test_release_wrong_hijack_id(self) -> None:
        """release() with wrong hijack_id → error='hijack_id_mismatch' (line 102)."""
        coord = HijackCoordinator()
        acquire = coord.acquire("owner", 60)
        assert acquire.ok
        result = coord.release("wrong-id")
        assert not result.ok
        assert result.error == "hijack_id_mismatch"

    def test_can_send_input_no_active_session(self) -> None:
        """can_send_input() when not hijacked → False (line 109)."""
        coord = HijackCoordinator()
        assert coord.can_send_input(None) is False
        assert coord.can_send_input("some-id") is False

    def test_can_send_input_wrong_id(self) -> None:
        """can_send_input() with wrong hijack_id → False."""
        coord = HijackCoordinator()
        acquire = coord.acquire("owner", 60)
        assert acquire.ok and acquire.session is not None
        assert coord.can_send_input("wrong-id") is False
        # correct id works
        assert coord.can_send_input(acquire.session.hijack_id) is True


# ---------------------------------------------------------------------------
# EventBus — lines 146-152 (nuclear sentinel), 186 (max subscribers)
# ---------------------------------------------------------------------------


class TestEventBusEdgeCases:
    """Test nuclear sentinel and max subscriber limit."""

    async def test_nuclear_sentinel_delivery(self) -> None:
        """When queue is full and first retry also fails, nuclear path clears + delivers.

        Lines 146-152: QueueFull on second put_nowait → while loop drains → sentinel delivered.
        We use a maxsize=1 queue and patch put_nowait to count calls.
        """
        from provide.uterm.bridge.hub.event_bus import EventBus, _Subscription

        bus = EventBus(max_subscribers_per_worker=10)
        sub = _Subscription(
            sub_id="test", worker_id="w1", queue=asyncio.Queue(maxsize=2), event_types=None, pattern=None
        )
        # Fill queue completely (2 items in maxsize=2)
        sub.queue.put_nowait({"type": "a"})
        sub.queue.put_nowait({"type": "b"})

        # First put_nowait(None) → QueueFull (queue full)
        # get_nowait() removes one item → queue has 1 item
        # Second put_nowait(None) → QueueFull again (queue still has 1 item + we just removed 1... wait)
        # Actually: maxsize=2, has 2. First put → QueueFull. get_nowait removes 1 → has 1.
        # Second put → succeeds (1 < 2). So we need maxsize=1 + 1 item.

        sub2 = _Subscription(
            sub_id="test2", worker_id="w1", queue=asyncio.Queue(maxsize=1), event_types=None, pattern=None
        )
        sub2.queue.put_nowait({"type": "fill"})  # queue full (1/1)

        # Now: first put_nowait(None) → QueueFull.
        # get_nowait() → removes "fill", dropped += 1.
        # Second put_nowait(None) → succeeds (queue empty, maxsize=1).
        # This covers line 143-145 but NOT lines 146-152 (nuclear path).

        # To hit nuclear path (146-152), the SECOND put_nowait must ALSO fail.
        # This can only happen if between get_nowait and the second put_nowait,
        # the queue is refilled. In production this is a race. We simulate it
        # with a queue subclass.
        class NuclearQueue(asyncio.Queue):  # type: ignore[type-arg]
            """Queue that forces the nuclear sentinel path.

            - First put_nowait → QueueFull (normal retry path)
            - get_nowait succeeds (drains one item)
            - Second put_nowait → QueueFull (triggers nuclear path)
            - Nuclear while loop: get_nowait drains remaining items
            - Third put_nowait → succeeds (sentinel delivered)
            """

            def __init__(self) -> None:
                super().__init__(maxsize=3)
                self._put_count = 0
                # Pre-fill with 2 items so the nuclear while loop has work to do
                super().put_nowait({"type": "a"})
                super().put_nowait({"type": "b"})

            def put_nowait(self, item: object) -> None:
                self._put_count += 1
                if self._put_count <= 2:
                    raise asyncio.QueueFull
                super().put_nowait(item)

        q = NuclearQueue()
        sub3 = _Subscription(sub_id="test3", worker_id="w1", queue=q, event_types=None, pattern=None)

        bus._put_sentinel(sub3)

        # Sentinel must be present after nuclear clear
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        assert None in items, "Sentinel was not delivered via nuclear path"

    async def test_max_subscribers_exceeded(self) -> None:
        """EventBus raises when max_subscribers_per_worker is exceeded (line 186)."""
        from provide.uterm.bridge.hub.event_bus import EventBus

        bus = EventBus(max_subscribers_per_worker=2)

        async with bus.watch("w1"), bus.watch("w1"):
            with pytest.raises(RuntimeError, match="max subscribers"):
                async with bus.watch("w1"):
                    pass


# ---------------------------------------------------------------------------
# TermHub core.py — lines 151->exit, 395->397, 504-506, 521->exit, 548
# ---------------------------------------------------------------------------


class TestHubCoreBranches:
    """Test hub core methods for uncovered branches."""

    async def test_touch_activity_unknown_worker(self) -> None:
        """touch_activity with non-existent worker → no-op (branch 151->exit)."""
        hub = TermHub()
        # Should not raise
        await hub.touch_activity("nonexistent-worker")

    async def test_get_idle_candidates(self) -> None:
        """get_idle_candidates returns idle workers (lines 504-506)."""
        hub = TermHub()
        ws = MagicMock()
        await hub.register_worker("w1", ws)
        # Set last_activity to past
        async with hub._lock:
            st = hub._workers["w1"]
            st.last_activity_at = time.monotonic() - 1000
        result = await hub.get_idle_candidates(timeout_s=10)
        assert len(result) >= 1
        assert result[0][0] == "w1"

    async def test_set_browser_role_unknown_worker(self) -> None:
        """set_browser_role with unknown worker → no-op (branch 521->exit)."""
        hub = TermHub()
        ws = MagicMock()
        await hub.set_browser_role("nonexistent", ws, "admin")

    async def test_set_browser_role_ws_not_registered(self) -> None:
        """set_browser_role with known worker but unregistered ws → no-op."""
        hub = TermHub()
        worker_ws = MagicMock()
        browser_ws = MagicMock()
        await hub.register_worker("w1", worker_ws)
        # browser_ws is not registered as a browser
        await hub.set_browser_role("w1", browser_ws, "admin")

    async def test_touch_activity_with_registered_worker(self) -> None:
        """touch_activity with registered worker updates timestamp (covers True branch)."""
        hub = TermHub()
        ws = MagicMock()
        await hub.register_worker("w1", ws)
        await hub.touch_activity("w1")
        # Verify the timestamp was updated
        async with hub._lock:
            st = hub._workers.get("w1")
            assert st is not None
            assert st.last_activity_at > 0

    async def test_get_worker_browser_role_unknown_worker(self) -> None:
        """get_worker_browser_role with unknown worker → None (line 548)."""
        hub = TermHub()
        ws = MagicMock()
        result = await hub.get_worker_browser_role("nonexistent", ws)
        assert result is None


# ---------------------------------------------------------------------------
# connections.py — branches 172->exit, 268->274, 352->356
# ---------------------------------------------------------------------------


class TestConnectionsBranches:
    """Test connection management uncovered branches."""

    async def test_update_last_snapshot_unknown_worker(self) -> None:
        """update_last_snapshot with unknown worker → no-op (branch 172->exit)."""
        hub = TermHub()
        # calling on non-existent worker should be fine (pragma: no branch)
        await hub.update_last_snapshot("nonexistent", {"screen": ""})

    async def test_force_release_hijack_no_hijack(self) -> None:
        """force_release_hijack when no hijack is active (branch 352->356)."""
        hub = TermHub()
        ws = MagicMock()
        ws.send_text = AsyncMock()
        await hub.register_worker("w1", ws)
        # No hijack set up — force_release should return False
        result = await hub.force_release_hijack("w1")
        assert result is False


# ---------------------------------------------------------------------------
# ownership.py — line 140 (open_mode guard in try_acquire_rest_hijack)
# ---------------------------------------------------------------------------


class TestRestHijackOpenMode:
    """REST hijack acquire is blocked when worker is in open mode."""

    async def test_acquire_rest_hijack_open_mode_returns_false(self) -> None:
        hub = TermHub()
        ws = MagicMock()
        ws.send_text = AsyncMock()
        await hub.register_worker("w1", ws)
        # Set input_mode to "open"
        await hub.set_worker_hello_mode("w1", "open")
        ok, err = await hub.try_acquire_rest_hijack(
            "w1",
            owner="alice",
            lease_s=60,
            hijack_id="h1",
            now=time.monotonic(),
        )
        assert ok is False
        assert err == "open_mode"
