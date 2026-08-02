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
from typing import Any

import httpx
from provide.uterm.client import connect_async_ws

from ._live_server import live_server_with_bus, wait_for_subscribers

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}


def _ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


def _sessions(n: int, prefix: str = "w") -> list[dict[str, Any]]:
    """Build *n* session config dicts with auto_start=False."""
    return [
        {
            "session_id": f"{prefix}{i}",
            "display_name": f"Worker {prefix}{i}",
            "connector_type": "shell",
            "auto_start": False,
        }
        for i in range(1, n + 1)
    ]


async def _drain_initial(ws: Any) -> None:
    """Drain the initial snapshot_req the server sends on worker connect."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(ws.recv(), timeout=2.0)


async def _await_collectors(hub: Any, worker_ids: list[str]) -> None:
    """Wait until the fanout send has subscribed an OutputCollector for every worker.

    The collector only captures output appended AFTER it subscribes to the EventBus,
    so injecting via ``hub.append_event`` after a fixed sleep races the subscription
    and silently drops output on a slow/loaded runner (the source of the flake).
    Delegates to the shared ``wait_for_subscribers`` helper for each worker.
    """
    for wid in worker_ids:
        await wait_for_subscribers(hub, wid, 1, stream="operation")


# ---------------------------------------------------------------------------
# 1. Parallel broadcast to 5 sessions — all receive same input
# ---------------------------------------------------------------------------


async def test_parallel_broadcast_5_sessions() -> None:
    """All 5 workers receive input and results are aggregated correctly."""
    sessions = _sessions(5)
    wids = [s["session_id"] for s in sessions]
    async with live_server_with_bus(sessions, label="fanout_parallel_5") as (hub, base_url):
        workers = []
        contexts = []
        for wid in wids:
            ctx = connect_async_ws(_ws_url(base_url, f"/ws/worker/{wid}/term"))
            ws = await ctx.__aenter__()
            contexts.append(ctx)
            workers.append(ws)
        try:
            for ws in workers:
                await _drain_initial(ws)

            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=15.0) as http:
                # Create group
                resp = await http.post(
                    "/api/fanout/groups",
                    json={
                        "name": "all-five",
                        "worker_ids": wids,
                        "mode": "parallel",
                    },
                )
                assert resp.status_code == 200
                group_id = resp.json()["group_id"]

                # Emit output once the fanout send has subscribed its collectors
                # (deterministic hand-off, not a racy fixed sleep).
                async def _emit() -> None:
                    await _await_collectors(hub, wids)
                    for wid in wids:
                        await hub.append_event(wid, "term", {"data": "hello world\n"})

                task = asyncio.create_task(_emit())
                resp = await http.post(
                    f"/api/fanout/groups/{group_id}/send",
                    json={
                        "data": "echo hello\n",
                        "quiesce_ms": 200,
                        "max_response_ms": 3000,
                    },
                )
                await task
                assert resp.status_code == 200
                body = resp.json()
                assert body["group_id"] == group_id
                assert len(body["results"]) == 5
                assert all(r["ok"] for r in body["results"])
                assert body["failed_sessions"] == []
        finally:
            for ctx in contexts:
                with contextlib.suppress(Exception):
                    await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 2. Parallel broadcast with 2 disconnected workers
# ---------------------------------------------------------------------------


async def test_parallel_broadcast_with_disconnected_workers() -> None:
    """3 connected, 2 not connected — failed_sessions has 2 entries."""
    sessions = _sessions(5, prefix="pd")
    wids = [s["session_id"] for s in sessions]
    connected_ids = wids[:3]
    disconnected_ids = wids[3:]

    async with live_server_with_bus(sessions, label="fanout_partial_disconnect") as (hub, base_url):
        contexts = []
        for wid in connected_ids:
            ctx = connect_async_ws(_ws_url(base_url, f"/ws/worker/{wid}/term"))
            ws = await ctx.__aenter__()
            contexts.append(ctx)
            await _drain_initial(ws)

        try:
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=15.0) as http:
                resp = await http.post(
                    "/api/fanout/groups",
                    json={
                        "name": "partial",
                        "worker_ids": wids,
                        "mode": "parallel",
                    },
                )
                assert resp.status_code == 200
                group_id = resp.json()["group_id"]

                async def _emit() -> None:
                    await _await_collectors(hub, connected_ids)
                    for wid in connected_ids:
                        await hub.append_event(wid, "term", {"data": "output\n"})

                task = asyncio.create_task(_emit())
                resp = await http.post(
                    f"/api/fanout/groups/{group_id}/send",
                    json={
                        "data": "cmd\n",
                        "quiesce_ms": 200,
                        "max_response_ms": 3000,
                    },
                )
                await task
                assert resp.status_code == 200
                body = resp.json()
                assert len(body["results"]) == 5
                ok_results = [r for r in body["results"] if r["ok"]]
                fail_results = [r for r in body["results"] if not r["ok"]]
                assert len(ok_results) == 3
                assert len(fail_results) == 2
                assert set(body["failed_sessions"]) == set(disconnected_ids)
        finally:
            for ctx in contexts:
                with contextlib.suppress(Exception):
                    await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 3. Sequential broadcast to 3 sessions — verify order
# ---------------------------------------------------------------------------


async def test_sequential_broadcast_order() -> None:
    """mode='sequential': workers are contacted one-by-one in order."""
    sessions = _sessions(3, prefix="sq")
    wids = [s["session_id"] for s in sessions]

    async with live_server_with_bus(sessions, label="fanout_sequential") as (hub, base_url):
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
                        "name": "seq-group",
                        "worker_ids": wids,
                        "mode": "sequential",
                    },
                )
                assert resp.status_code == 200
                group_id = resp.json()["group_id"]

                # For sequential mode, the controller processes workers one at a
                # time: send_worker → subscribe → collect.  We watch the EventBus
                # subscriber count to know when each worker's collector has
                # subscribed, then inject output for that worker.
                bus = hub._operation_event_bus

                async def _emit_sequential() -> None:
                    for wid in wids:
                        deadline = asyncio.get_running_loop().time() + 5.0
                        while asyncio.get_running_loop().time() < deadline:
                            if bus is not None and bus.subscriber_count(wid) > 0:
                                break
                            await asyncio.sleep(0.005)
                        await hub.append_event(wid, "term", {"data": f"output-{wid}\n"})

                task = asyncio.create_task(_emit_sequential())
                resp = await http.post(
                    f"/api/fanout/groups/{group_id}/send",
                    json={
                        "data": "cmd\n",
                        "quiesce_ms": 200,
                        "max_response_ms": 8000,
                    },
                )
                await task
                assert resp.status_code == 200
                body = resp.json()
                assert len(body["results"]) == 3
                assert all(r["ok"] for r in body["results"])
                # Verify each worker got output containing its worker id
                for r in body["results"]:
                    assert r["output_delta"] is not None
                    assert f"output-{r['worker_id']}" in r["output_delta"]
        finally:
            for ctx in contexts:
                with contextlib.suppress(Exception):
                    await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 4. Sequential stop-on-error — second hits error, third skipped
# ---------------------------------------------------------------------------


async def test_sequential_stop_on_error() -> None:
    """stop_on_first_error=True: second worker error stops execution, third is skipped."""
    sessions = _sessions(3, prefix="se")
    wids = [s["session_id"] for s in sessions]

    async with live_server_with_bus(sessions, label="fanout_stop_on_error") as (hub, base_url):
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
                        "name": "stop-err",
                        "worker_ids": wids,
                        "mode": "sequential",
                        "stop_on_first_error": True,
                        "error_pattern": "FATAL ERROR",
                    },
                )
                assert resp.status_code == 200
                group_id = resp.json()["group_id"]

                bus = hub._operation_event_bus

                async def _emit() -> None:
                    # Wait for worker 1's collector to subscribe, then emit success
                    deadline = asyncio.get_running_loop().time() + 5.0
                    while asyncio.get_running_loop().time() < deadline:
                        if bus is not None and bus.subscriber_count(wids[0]) > 0:
                            break
                        await asyncio.sleep(0.005)
                    await hub.append_event(wids[0], "term", {"data": "ok\n"})
                    # Wait for worker 2's collector to subscribe, then emit error
                    deadline = asyncio.get_running_loop().time() + 5.0
                    while asyncio.get_running_loop().time() < deadline:
                        if bus is not None and bus.subscriber_count(wids[1]) > 0:
                            break
                        await asyncio.sleep(0.005)
                    await hub.append_event(wids[1], "term", {"data": "FATAL ERROR: something broke\n"})

                task = asyncio.create_task(_emit())
                resp = await http.post(
                    f"/api/fanout/groups/{group_id}/send",
                    json={
                        "data": "run\n",
                        "quiesce_ms": 200,
                        "max_response_ms": 5000,
                    },
                )
                await task
                assert resp.status_code == 200
                body = resp.json()
                assert len(body["results"]) == 3
                # First: ok
                assert body["results"][0]["ok"] is True
                # Second: ok (ran but had error output)
                assert body["results"][1]["ok"] is True
                assert "FATAL ERROR" in (body["results"][1]["output_delta"] or "")
                # Third: skipped (failed because stopped)
                assert body["results"][2]["ok"] is False
                assert wids[2] in body["failed_sessions"]
        finally:
            for ctx in contexts:
                with contextlib.suppress(Exception):
                    await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 5. Divergence detection — 4 similar, 1 different
# ---------------------------------------------------------------------------


async def test_divergence_detection() -> None:
    """4 sessions return similar output, 1 returns different — divergent_sessions populated."""
    sessions = _sessions(5, prefix="dv")
    wids = [s["session_id"] for s in sessions]

    async with live_server_with_bus(sessions, label="fanout_divergence") as (hub, base_url):
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
                        "name": "diverge",
                        "worker_ids": wids,
                        "mode": "parallel",
                        "divergence_threshold": 0.8,
                    },
                )
                assert resp.status_code == 200
                group_id = resp.json()["group_id"]

                async def _emit() -> None:
                    await _await_collectors(hub, wids)
                    # 4 workers: similar output
                    for wid in wids[:4]:
                        await hub.append_event(
                            wid, "term", {"data": "total 48\ndrwxr-xr-x 12 user staff 384 Apr 7 main.py\n"}
                        )
                    # 1 worker: completely different output
                    await hub.append_event(
                        wids[4], "term", {"data": "ERROR: permission denied\nSegfault at 0xDEADBEEF\n"}
                    )

                task = asyncio.create_task(_emit())
                resp = await http.post(
                    f"/api/fanout/groups/{group_id}/send",
                    json={
                        "data": "ls -la\n",
                        "quiesce_ms": 200,
                        "max_response_ms": 3000,
                    },
                )
                await task
                assert resp.status_code == 200
                body = resp.json()
                assert len(body["results"]) == 5
                assert all(r["ok"] for r in body["results"])
                # The divergent worker should be flagged
                assert len(body["divergent_sessions"]) >= 1
                assert wids[4] in body["divergent_sessions"]
        finally:
            for ctx in contexts:
                with contextlib.suppress(Exception):
                    await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 6. Group CRUD lifecycle — create, list, get, grant, delete
# ---------------------------------------------------------------------------
