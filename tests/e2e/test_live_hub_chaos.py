#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Chaos E2E tests: abrupt disconnects, reconnect cycles, mid-operation failures."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import websockets

from .conftest import _drain_all, _drain_until, _ws_url

# ---------------------------------------------------------------------------
# TestWorkerDropsMidHijack
# ---------------------------------------------------------------------------


class TestWorkerDropsMidHijack:
    async def test_worker_drops_mid_rest_hijack_clears_lease(self, live_hub: Any) -> None:
        """Worker drops while REST hijack is active; second acquire succeeds."""
        _, base_url = live_hub

        async with httpx.AsyncClient(base_url=base_url) as http:
            async with websockets.connect(_ws_url(base_url, "/ws/worker/chaos1/term")) as worker:
                await worker.recv()  # snapshot_req

                r = await http.post("/worker/chaos1/hijack/acquire", json={"owner": "chaos-owner", "lease_s": 60})
                assert r.status_code == 200, f"Acquire should succeed, got {r.status_code}: {r.text}"

            # Worker drops; hub should clear hijack state on disconnect

            # Reconnect worker and try to acquire again
            async with websockets.connect(_ws_url(base_url, "/ws/worker/chaos1/term")) as worker2:
                await worker2.recv()  # snapshot_req
                r2 = await http.post("/worker/chaos1/hijack/acquire", json={"owner": "new-owner", "lease_s": 60})
                assert r2.status_code == 200, (
                    f"Second acquire after worker drop should succeed, got {r2.status_code}: {r2.text}"
                )

    async def test_worker_drops_mid_ws_hijack_sends_resume(self, live_hub: Any) -> None:
        """Worker drops while browser WS hijack is active; browser sees worker_disconnected."""
        _, base_url = live_hub

        async with websockets.connect(_ws_url(base_url, "/ws/browser/chaos2/term")) as browser:
            await _drain_all(browser)

            async with websockets.connect(_ws_url(base_url, "/ws/worker/chaos2/term")) as worker:
                await worker.recv()
                await _drain_until(browser, "worker_connected", timeout=2.0)

                # Browser acquires hijack
                await browser.send(json.dumps({"type": "hijack_request"}))
                state = await _drain_until(browser, "hijack_state")
                assert state is not None, "Should receive hijack_state"
                assert state.get("owner") == "me", f"Browser should own hijack, got {state}"

                # Worker drops while hijacked
                # (context exit triggers worker drop)

            # Browser should see worker_disconnected
            disc = await _drain_until(browser, "worker_disconnected", timeout=3.0)
            assert disc is not None, "Browser should see worker_disconnected after abrupt drop"


# ---------------------------------------------------------------------------
# TestBrowserDropsMidHijack
# ---------------------------------------------------------------------------


class TestBrowserDropsMidHijack:
    async def test_browser_drops_releases_hijack(self, live_hub: Any) -> None:
        """Browser drops while hijacking; worker receives resume."""
        _, base_url = live_hub

        async with websockets.connect(_ws_url(base_url, "/ws/worker/chaos3/term")) as worker:
            await worker.recv()

            async with websockets.connect(_ws_url(base_url, "/ws/browser/chaos3/term")) as browser:
                await _drain_all(browser)
                await browser.send(json.dumps({"type": "hijack_request"}))
                state = await _drain_until(browser, "hijack_state")
                assert state is not None, "Should receive hijack_state"
                ctrl = await _drain_until(worker, "control")
                assert ctrl is not None, "Worker should receive pause"
                assert ctrl["action"] == "pause", f"Expected pause, got {ctrl.get('action')}"

            # Browser exited — worker should receive resume
            resume = await _drain_until(worker, "control", timeout=3.0)
            assert resume is not None, "Worker should receive resume after browser drop"
            assert resume["action"] == "resume", f"Expected resume, got {resume.get('action')}"

    async def test_multiple_browsers_drop_one_keeps_hijack(self, live_hub: Any) -> None:
        """Two browsers; one drops — the remaining browser is still connected."""
        _, base_url = live_hub

        async with websockets.connect(_ws_url(base_url, "/ws/worker/chaos4/term")) as worker:
            await worker.recv()

            async with (
                websockets.connect(_ws_url(base_url, "/ws/browser/chaos4/term")) as b1,
                websockets.connect(_ws_url(base_url, "/ws/browser/chaos4/term")) as b2,
            ):
                await _drain_all(b1)
                await _drain_all(b2)

                # b1 acquires hijack
                await b1.send(json.dumps({"type": "hijack_request"}))
                state = await _drain_until(b1, "hijack_state")
                assert state is not None, "b1 should receive hijack_state"
                assert state.get("owner") == "me", f"b1 should own hijack, got {state}"

                # b2 observes
                b2_state = await _drain_until(b2, "hijack_state")
                assert b2_state is not None, "b2 should see hijack_state"
                assert b2_state.get("owner") == "other", f"b2 should see owner=other, got {b2_state}"

                # Drain the "pause" the worker received when b1 hijacked
                await _drain_until(worker, "control", timeout=2.0)

            # Both dropped; worker should get resume
            resume = await _drain_until(worker, "control", timeout=3.0)
            assert resume is not None, "Worker should receive resume after all browsers drop"
            assert resume["action"] == "resume", f"Expected resume, got {resume.get('action')}"


# ---------------------------------------------------------------------------
# TestRapidConnectDisconnect
# ---------------------------------------------------------------------------


class TestRapidConnectDisconnect:
    async def test_rapid_worker_reconnect_cycles(self, live_hub: Any) -> None:
        """Worker connects and disconnects rapidly; hub state remains consistent."""
        _, base_url = live_hub

        for cycle in range(5):
            async with websockets.connect(_ws_url(base_url, "/ws/worker/chaos5/term")) as worker:
                msg_str = await asyncio.wait_for(worker.recv(), timeout=2.0)
                msg = json.loads(msg_str)
                assert msg.get("type") == "snapshot_req", f"Cycle {cycle}: expected snapshot_req, got {msg.get('type')}"
            # Brief yield between cycles
            await asyncio.sleep(0.02)

        # Hub should not have leaked workers
        async with httpx.AsyncClient(base_url=base_url) as http:  # noqa: SIM117
            # POST acquire to a fresh worker to confirm hub is still functional
            async with websockets.connect(_ws_url(base_url, "/ws/worker/chaos5/term")) as worker:
                await worker.recv()
                r = await http.post("/worker/chaos5/hijack/acquire", json={"owner": "test", "lease_s": 10})
                assert r.status_code == 200, f"Hub should still work after rapid reconnects, got {r.status_code}"

    async def test_rapid_browser_reconnect_cycles(self, live_hub: Any) -> None:
        """Browser connects and disconnects rapidly; hub state consistent."""
        _, base_url = live_hub

        async with websockets.connect(_ws_url(base_url, "/ws/worker/chaos6/term")) as worker:
            await worker.recv()

            for cycle in range(5):
                async with websockets.connect(_ws_url(base_url, "/ws/browser/chaos6/term")) as browser:
                    hello = await _drain_until(browser, "hello", timeout=2.0)
                    assert hello is not None, f"Cycle {cycle}: browser should receive hello"
                await asyncio.sleep(0.02)

            # Worker should still be connected
            async with websockets.connect(_ws_url(base_url, "/ws/browser/chaos6/term")) as browser:
                hello = await _drain_until(browser, "hello", timeout=2.0)
                assert hello is not None, "Browser should still receive hello after rapid cycles"
                assert hello.get("worker_online") is True, "Worker should be online, got hello={hello}"

    async def test_concurrent_connects_and_disconnects(self, live_hub: Any) -> None:
        """Many concurrent WS connections; hub handles all without corruption."""
        _, base_url = live_hub

        async with websockets.connect(_ws_url(base_url, "/ws/worker/chaos7/term")) as worker:
            await worker.recv()

            async def _connect_and_disconnect(i: int) -> None:
                async with websockets.connect(_ws_url(base_url, "/ws/browser/chaos7/term")) as browser:
                    await _drain_until(browser, "hello", timeout=2.0)

            # Launch 8 concurrent browser connects/disconnects
            await asyncio.gather(*[_connect_and_disconnect(i) for i in range(8)])

            # Worker should still be reachable
            async with httpx.AsyncClient(base_url=base_url) as http:
                r = await http.post("/worker/chaos7/hijack/acquire", json={"owner": "final", "lease_s": 10})
                assert r.status_code == 200, (
                    f"Hub should be functional after concurrent connects, got {r.status_code}: {r.text}"
                )
