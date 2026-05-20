#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E rate-limiting tests: REST acquire/send buckets, recovery, concurrency."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI

from provide.uterm.bridge.hub import TermHub
from provide.uterm.client import connect_async_ws

from .conftest import _drain_all, _drain_until, _snapshot_msg, _wait_for_server, _ws_url


@asynccontextmanager
async def _tight_rate_hub(acquire_rate: float = 1.0, send_rate: float = 2.0):
    """Context manager: hub with custom rate limits for testing."""
    hub = TermHub(
        resolve_browser_role=lambda _ws, _worker_id: "admin",
        rest_acquire_rate_limit_per_sec=acquire_rate,
        rest_send_rate_limit_per_sec=send_rate,
    )
    app = FastAPI()
    app.include_router(hub.create_router())

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    try:
        await _wait_for_server(server, task, "rate_hub")
        port: int = server.servers[0].sockets[0].getsockname()[1]
        yield hub, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)


class TestRestAcquireRateLimit:
    """REST acquire endpoint rate limiting."""

    async def test_rapid_acquire_triggers_429(self) -> None:
        """Fire 10 acquire requests rapidly; at least one gets 429."""
        async with _tight_rate_hub(acquire_rate=1.0) as (_hub, base_url):
            async with (
                connect_async_ws(_ws_url(base_url, "/ws/worker/rrl1/term")) as worker,
                httpx.AsyncClient(base_url=base_url) as http,
            ):
                await worker.recv()  # snapshot_req

                async def acquire():
                    r = await http.post(
                        "/worker/rrl1/hijack/acquire",
                        json={"owner": "test", "lease_s": 60},
                    )
                    return r.status_code

                # Fire 10 requests concurrently (will deplete bucket at 1.0/s)
                results = await asyncio.gather(*[acquire() for _ in range(10)])
                statuses = set(results)

                # With rate=1.0/sec and burst, expect at least one 429
                assert 429 in statuses, f"Expected at least one 429, got statuses: {statuses}"

    async def test_acquire_rate_limit_recovers(self) -> None:
        """Hit limit, sleep 1.1s, verify next acquire succeeds."""
        async with _tight_rate_hub(acquire_rate=1.0) as (_hub, base_url):
            async with (
                connect_async_ws(_ws_url(base_url, "/ws/worker/rrl2/term")) as worker,
                httpx.AsyncClient(base_url=base_url) as http,
            ):
                await worker.recv()

                # Fire requests to deplete bucket
                for _ in range(5):
                    await http.post(
                        "/worker/rrl2/hijack/acquire",
                        json={"owner": "test", "lease_s": 60},
                    )

                # Should get a 429 at some point (bucket exhausted)
                r_limited = await http.post(
                    "/worker/rrl2/hijack/acquire",
                    json={"owner": "test", "lease_s": 60},
                )
                assert r_limited.status_code == 429, (
                    f"Expected 429 after bucket exhaustion, got {r_limited.status_code}"
                )

                # Wait for recovery (1 token per second at rate=1.0)
                # This is time-based; no polling alternative for token bucket recovery
                await asyncio.sleep(1.5)

                # Should succeed now
                r_success = await http.post(
                    "/worker/rrl2/hijack/acquire",
                    json={"owner": "test2", "lease_s": 60},
                )
                # Expect 200 (success) or 409 (existing hijack from earlier request)
                assert r_success.status_code in (200, 409), (
                    f"After recovery should return 200 or 409, got {r_success.status_code}: {r_success.text}"
                )


class TestRestSendRateLimit:
    """REST send endpoint rate limiting."""

    async def test_rapid_send_triggers_429(self) -> None:
        """Acquire once, fire 20 send requests; at least one gets 429."""
        async with _tight_rate_hub(send_rate=2.0) as (_hub, base_url):
            async with (
                connect_async_ws(_ws_url(base_url, "/ws/worker/rrl3/term")) as worker,
                httpx.AsyncClient(base_url=base_url) as http,
            ):
                await worker.recv()

                # Acquire once
                r = await http.post(
                    "/worker/rrl3/hijack/acquire",
                    json={"owner": "test", "lease_s": 60},
                )
                assert r.status_code == 200
                hijack_id = r.json()["hijack_id"]
                await _drain_until(worker, "control")

                # Send snapshot so guard passes; drain_until replaces sleep
                await worker.send(json.dumps(_snapshot_msg()))
                await _drain_until(worker, "snapshot", timeout=1.0)

                async def send_key():
                    r = await http.post(
                        f"/worker/rrl3/hijack/{hijack_id}/send",
                        json={"keys": "x", "timeout_ms": 500},
                    )
                    return r.status_code

                # Fire 20 sends concurrently (will deplete bucket at 2.0/s)
                results = await asyncio.gather(*[send_key() for _ in range(20)])
                statuses = set(results)

                # With rate=2.0/sec and burst, expect at least one 429
                assert 429 in statuses, f"Expected at least one 429, got statuses: {statuses}"

    async def test_step_respects_send_bucket(self) -> None:
        """Rapid POST /step calls also hit send rate limit."""
        async with _tight_rate_hub(send_rate=1.0) as (_hub, base_url):
            async with (
                connect_async_ws(_ws_url(base_url, "/ws/worker/rrl4/term")) as worker,
                httpx.AsyncClient(base_url=base_url) as http,
            ):
                await worker.recv()

                r = await http.post(
                    "/worker/rrl4/hijack/acquire",
                    json={"owner": "test", "lease_s": 60},
                )
                hijack_id = r.json()["hijack_id"]
                await _drain_until(worker, "control")

                # Send snapshot; drain_until replaces sleep
                await worker.send(json.dumps(_snapshot_msg()))
                await _drain_until(worker, "snapshot", timeout=1.0)

                async def step():
                    r = await http.post(f"/worker/rrl4/hijack/{hijack_id}/step")
                    return r.status_code

                # Fire 5 step requests concurrently
                results = await asyncio.gather(*[step() for _ in range(5)])
                statuses = set(results)

                # With rate=1.0/sec, expect at least one 429
                assert 429 in statuses, f"Expected at least one 429 from step rate limiting, got statuses: {statuses}"


class TestBrowserWsRateLimit:
    """WebSocket browser message rate limiting (silent drop)."""

    async def test_rapid_browser_input_silently_dropped(self) -> None:
        """Browser fires >30 input msgs/sec; worker receives fewer (hub silently drops)."""
        async with _tight_rate_hub() as (_hub, base_url):
            async with (
                connect_async_ws(_ws_url(base_url, "/ws/browser/rrl5/term")) as browser,
                connect_async_ws(_ws_url(base_url, "/ws/worker/rrl5/term")) as worker,
            ):
                await worker.recv()
                await _drain_all(browser)

                # Switch to open mode; wait for mode propagation to browser
                await worker.send(
                    json.dumps(
                        {
                            "type": "worker_hello",
                            "input_mode": "open",
                            "ts": time.time(),
                        }
                    )
                )
                await _drain_until(browser, "hijack_state", timeout=2.0)

                # Fire 50 input messages rapidly from browser
                for i in range(50):
                    await browser.send(json.dumps({"type": "input", "data": f"key{i}"}))

                # Drain all input messages received by worker
                msgs = await _drain_all(worker, timeout=0.7)
                input_msgs = [m for m in msgs if m.get("type") == "input"]

                # Should receive fewer than 50 (hub rate-limits to ~30/sec, silently drops)
                assert len(input_msgs) < 50, (
                    f"Expected fewer than 50 inputs due to rate limiting, got {len(input_msgs)}"
                )
