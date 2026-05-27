#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E tests for the fan-out feature — full-stack through the HTTP API.

Each test spins up a live uvicorn server with dev auth, connects workers via
WebSocket, and exercises the fan-out REST API (create group, send, delete, etc.).
Output is injected via ``hub.append_event()`` to feed the ``OutputCollector``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import httpx
from provide.uterm.client import connect_async_ws

from tests.e2e._live_server import live_server_with_bus

from .test_fanout_e2e_part1 import _drain_initial, _sessions, _ws_url

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}


async def test_large_output_collection() -> None:
    """Workers send >10KB of output each — correctly aggregated."""
    sessions = _sessions(3, prefix="lg")
    wids = [s["session_id"] for s in sessions]
    large_chunk = "A" * 5000  # 5KB per event, 3 events = 15KB

    async with live_server_with_bus(sessions, label="fanout_large_output") as (hub, base_url):
        contexts = []
        for wid in wids:
            ctx = connect_async_ws(_ws_url(base_url, f"/ws/worker/{wid}/term"))
            ws = await ctx.__aenter__()
            contexts.append(ctx)
            await _drain_initial(ws)

        try:
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=15.0) as http:
                resp = await http.post(
                    "/api/fanout/groups",
                    json={
                        "name": "large",
                        "worker_ids": wids,
                        "mode": "parallel",
                    },
                )
                assert resp.status_code == 200
                group_id = resp.json()["group_id"]

                async def _emit() -> None:
                    await asyncio.sleep(0.03)
                    for wid in wids:
                        # Send 3 chunks of 5KB each = 15KB per worker
                        for _ in range(3):
                            await hub.append_event(wid, "term", {"data": large_chunk})
                            await asyncio.sleep(0.01)

                task = asyncio.create_task(_emit())
                resp = await http.post(
                    f"/api/fanout/groups/{group_id}/send",
                    json={
                        "data": "generate\n",
                        "quiesce_ms": 300,
                        "max_response_ms": 5000,
                    },
                )
                await task
                assert resp.status_code == 200
                body = resp.json()
                assert len(body["results"]) == 3
                for r in body["results"]:
                    assert r["ok"]
                    # At least 10KB of output collected
                    assert len(r["output_delta"]) >= 10000, f"Expected >= 10KB, got {len(r['output_delta'])} bytes"
        finally:
            for ctx in contexts:
                with contextlib.suppress(Exception):
                    await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 13. Adaptive quiesce timing — fast return when output stops quickly
# ---------------------------------------------------------------------------


async def test_adaptive_quiesce_fast_return() -> None:
    """quiesce_ms=100: returns quickly when output stops immediately."""
    sessions = _sessions(3, prefix="aq")
    wids = [s["session_id"] for s in sessions]

    async with live_server_with_bus(sessions, label="fanout_quiesce") as (hub, base_url):
        contexts = []
        for wid in wids:
            ctx = connect_async_ws(_ws_url(base_url, f"/ws/worker/{wid}/term"))
            ws = await ctx.__aenter__()
            contexts.append(ctx)
            await _drain_initial(ws)

        try:
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=15.0) as http:
                resp = await http.post(
                    "/api/fanout/groups",
                    json={
                        "name": "quiesce",
                        "worker_ids": wids,
                        "mode": "parallel",
                    },
                )
                assert resp.status_code == 200
                group_id = resp.json()["group_id"]

                # Output arrives immediately then stops
                async def _emit() -> None:
                    await asyncio.sleep(0.02)
                    for wid in wids:
                        await hub.append_event(wid, "term", {"data": "quick\n"})

                start = time.monotonic()
                task = asyncio.create_task(_emit())
                resp = await http.post(
                    f"/api/fanout/groups/{group_id}/send",
                    json={
                        "data": "fast\n",
                        "quiesce_ms": 100,
                        "max_response_ms": 10000,
                    },
                )
                await task
                elapsed = time.monotonic() - start

                assert resp.status_code == 200
                body = resp.json()
                assert all(r["ok"] for r in body["results"])
                # Should return well before the 10s max_response_ms
                assert elapsed < 3.0, f"Expected fast return, took {elapsed:.2f}s"
        finally:
            for ctx in contexts:
                with contextlib.suppress(Exception):
                    await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 14. Send to non-existent group → 404
# ---------------------------------------------------------------------------


async def test_send_to_nonexistent_group() -> None:
    """Sending to a non-existent group returns 404."""
    sessions = _sessions(1, prefix="ne")

    async with live_server_with_bus(sessions, label="fanout_404") as (_hub, base_url):
        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
            resp = await http.post(
                "/api/fanout/groups/nonexistent-id/send",
                json={
                    "data": "hello\n",
                    "quiesce_ms": 100,
                    "max_response_ms": 1000,
                },
            )
            assert resp.status_code == 404
            assert "not found" in resp.json()["error"]


# ---------------------------------------------------------------------------
# 15. Delete non-existent group → 404
# ---------------------------------------------------------------------------


async def test_delete_nonexistent_group() -> None:
    """Deleting a non-existent group returns 404."""
    sessions = _sessions(1, prefix="dn")

    async with live_server_with_bus(sessions, label="fanout_del_404") as (_hub, base_url):
        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
            resp = await http.delete("/api/fanout/groups/does-not-exist")
            assert resp.status_code == 404
            assert "not found" in resp.json()["error"]
