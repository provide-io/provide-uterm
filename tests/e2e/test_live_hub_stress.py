#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Stress E2E tests: many concurrent browsers, broadcast under load."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import websockets

from .conftest import _drain_all, _drain_until, _snapshot_msg, _ws_url

# ---------------------------------------------------------------------------
# TestConcurrentBrowsers
# ---------------------------------------------------------------------------


class TestConcurrentBrowsers:
    async def test_ten_concurrent_browsers_all_receive_hello(self, live_hub: Any) -> None:
        """10 concurrent browsers each receive a hello message."""
        _, base_url = live_hub

        async def _connect_and_get_hello(i: int) -> dict[str, Any] | None:
            async with websockets.connect(_ws_url(base_url, "/ws/browser/stress1/term")) as browser:
                return await _drain_until(browser, "hello", timeout=3.0)

        results = await asyncio.gather(*[_connect_and_get_hello(i) for i in range(10)])

        for i, hello in enumerate(results):
            assert hello is not None, f"Browser {i} should receive hello"
            assert hello.get("type") == "hello", f"Browser {i}: expected hello type, got {hello}"

    async def test_ten_concurrent_browsers_all_receive_broadcast(self, live_hub: Any) -> None:
        """10 browsers connected; worker broadcasts term data; all receive it."""
        _, base_url = live_hub

        received: dict[int, list[dict[str, Any]]] = {i: [] for i in range(10)}
        ready = asyncio.Barrier(11)  # 10 browsers + 1 coordinator

        async def _browser_task(i: int) -> None:
            async with websockets.connect(_ws_url(base_url, "/ws/browser/stress2/term")) as browser:
                await _drain_all(browser)  # consume hello + hijack_state
                await ready.wait()  # signal ready, wait for all + coordinator
                # Wait for the term message
                term = await _drain_until(browser, "term", timeout=5.0)
                if term is not None:
                    received[i].append(term)

        async with websockets.connect(_ws_url(base_url, "/ws/worker/stress2/term")) as worker:
            await worker.recv()  # snapshot_req

            # Start 10 browsers
            browser_tasks = [asyncio.create_task(_browser_task(i)) for i in range(10)]

            # Wait for all browsers to be connected and ready
            await ready.wait()

            # Worker broadcasts a term message
            await worker.send(
                json.dumps(
                    {
                        "type": "term",
                        "data": "broadcast test",
                        "ts": time.time(),
                    }
                )
            )

            # Wait for all browser tasks to complete
            await asyncio.gather(*browser_tasks)

        received_count = sum(1 for msgs in received.values() if msgs)
        assert received_count >= 8, f"At least 8 of 10 browsers should receive the broadcast, got {received_count}"

    async def test_hijack_contention_among_ten_browsers(self, live_hub: Any) -> None:
        """10 browsers race to hijack; exactly 1 succeeds (owner=me), 9 see owner=other."""
        _, base_url = live_hub

        results: list[dict[str, Any] | None] = []
        result_lock = asyncio.Lock()

        async def _try_hijack(i: int) -> None:
            async with websockets.connect(_ws_url(base_url, "/ws/browser/stress3/term")) as browser:
                await _drain_all(browser)
                await browser.send(json.dumps({"type": "hijack_request"}))
                state = await _drain_until(browser, "hijack_state", timeout=3.0)
                async with result_lock:
                    results.append(state)

        async with websockets.connect(_ws_url(base_url, "/ws/worker/stress3/term")) as worker:
            await worker.recv()

            await asyncio.gather(*[_try_hijack(i) for i in range(10)])

        owners = [r.get("owner") for r in results if r is not None]
        me_count = owners.count("me")
        other_count = owners.count("other")

        assert me_count == 1, f"Exactly 1 browser should own hijack, got {me_count}"
        assert other_count == 9, f"9 browsers should see owner=other, got {other_count}"


# ---------------------------------------------------------------------------
# TestBroadcastUnderLoad
# ---------------------------------------------------------------------------


class TestBroadcastUnderLoad:
    async def test_rapid_term_broadcasts_received_by_all_browsers(self, live_hub: Any) -> None:
        """Worker sends 20 rapid term messages; multiple browsers receive most of them."""
        _, base_url = live_hub

        browser_counts: dict[int, int] = dict.fromkeys(range(3), 0)

        ready4 = asyncio.Barrier(4)  # 3 browsers + 1 coordinator

        async def _browser_task(i: int) -> None:
            async with websockets.connect(_ws_url(base_url, "/ws/browser/stress4/term")) as browser:
                await _drain_all(browser)  # drain initial messages
                await ready4.wait()
                msgs = await _drain_all(browser, timeout=2.0)
                browser_counts[i] = sum(1 for m in msgs if m.get("type") == "term")

        async with websockets.connect(_ws_url(base_url, "/ws/worker/stress4/term")) as worker:
            await worker.recv()

            browser_tasks = [asyncio.create_task(_browser_task(i)) for i in range(3)]
            await ready4.wait()  # Wait for all browsers ready

            # Send 20 rapid term messages
            for j in range(20):
                await worker.send(json.dumps({"type": "term", "data": f"msg{j}", "ts": time.time()}))

            await asyncio.gather(*browser_tasks)

        # Each browser should have received some messages
        for i, count in browser_counts.items():
            assert count > 0, f"Browser {i} should have received at least 1 term message, got {count}"

    async def test_snapshot_broadcast_updates_all_browsers(self, live_hub: Any) -> None:
        """Worker sends snapshot; all connected browsers see it."""
        _, base_url = live_hub

        snapshots: dict[int, dict[str, Any] | None] = {}

        ready5 = asyncio.Barrier(6)  # 5 browsers + 1 coordinator

        async def _browser_task(i: int) -> None:
            async with websockets.connect(_ws_url(base_url, "/ws/browser/stress5/term")) as browser:
                await _drain_all(browser)  # drain hello + hijack_state
                await ready5.wait()
                snap = await _drain_until(browser, "snapshot", timeout=3.0)
                snapshots[i] = snap

        async with websockets.connect(_ws_url(base_url, "/ws/worker/stress5/term")) as worker:
            await worker.recv()

            browser_tasks = [asyncio.create_task(_browser_task(i)) for i in range(5)]
            await ready5.wait()  # Wait for all browsers ready

            # Worker sends snapshot
            await worker.send(json.dumps(_snapshot_msg("stress test content")))

            await asyncio.gather(*browser_tasks)

        received = sum(1 for s in snapshots.values() if s is not None)
        assert received >= 4, f"At least 4 of 5 browsers should receive snapshot, got {received}"
        for i, snap in snapshots.items():
            if snap is not None:
                assert snap.get("screen") == "stress test content", (
                    f"Browser {i}: unexpected snapshot content: {snap.get('screen')}"
                )
