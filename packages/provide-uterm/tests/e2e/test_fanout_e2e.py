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
from typing import Any

import httpx
from provide.terminal.client import connect_async_ws

from tests.e2e._live_server import live_server_with_bus

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

                # Emit output concurrently after a small delay
                async def _emit() -> None:
                    await asyncio.sleep(0.05)
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
                    await asyncio.sleep(0.05)
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
                bus = hub.event_bus

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

                bus = hub.event_bus

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
                    await asyncio.sleep(0.05)
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


async def test_group_crud_lifecycle() -> None:
    """Full lifecycle: create, list, grant, delete."""
    sessions = _sessions(3, prefix="cr")
    wids = [s["session_id"] for s in sessions]

    async with live_server_with_bus(sessions, label="fanout_crud") as (_hub, base_url):
        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
            # Create
            resp = await http.post(
                "/api/fanout/groups",
                json={
                    "name": "crud-group",
                    "worker_ids": wids,
                    "mode": "parallel",
                },
            )
            assert resp.status_code == 200
            group_id = resp.json()["group_id"]
            assert resp.json()["name"] == "crud-group"
            assert resp.json()["session_count"] == 3

            # List
            resp = await http.get("/api/fanout/groups")
            assert resp.status_code == 200
            groups = resp.json()
            assert len(groups) >= 1
            names = [g["name"] for g in groups]
            assert "crud-group" in names

            # Grant
            resp = await http.post(
                f"/api/fanout/groups/{group_id}/grants",
                json={
                    "grantee": "bob",
                },
            )
            assert resp.status_code == 204

        # List as grantee — fresh client with bob's identity
        async with httpx.AsyncClient(
            base_url=base_url,
            headers={"X-Uterm-Principal": "bob", "X-Uterm-Role": "admin"},
            timeout=10.0,
        ) as bob_http:
            resp = await bob_http.get("/api/fanout/groups")
            assert resp.status_code == 200
            bob_groups = resp.json()
            assert any(g["group_id"] == group_id for g in bob_groups)

        # Delete and verify — individual requests to avoid stale connection reuse
        transport = httpx.AsyncHTTPTransport(retries=2)
        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0, transport=transport) as http:
            resp = await http.delete(f"/api/fanout/groups/{group_id}")
            assert resp.status_code == 204

        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0, transport=transport) as http:
            resp = await http.get("/api/fanout/groups")
            assert resp.status_code == 200
            remaining = resp.json()
            assert not any(g["group_id"] == group_id for g in remaining)


# ---------------------------------------------------------------------------
# 7. Max group size enforcement — 60 sessions → 400
# ---------------------------------------------------------------------------


async def test_max_group_size_enforcement() -> None:
    """Creating a group with 60 workers exceeds the 50-session max → 400."""
    sessions = _sessions(3, prefix="mx")

    async with live_server_with_bus(sessions, label="fanout_max_size") as (_hub, base_url):
        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
            worker_ids = [f"fake-{i}" for i in range(60)]
            resp = await http.post(
                "/api/fanout/groups",
                json={
                    "name": "too-big",
                    "worker_ids": worker_ids,
                    "mode": "parallel",
                },
            )
            assert resp.status_code == 400
            assert "exceeds max" in resp.json()["error"]


# ---------------------------------------------------------------------------
# 8. Empty input broadcast — no crash
# ---------------------------------------------------------------------------


async def test_empty_input_broadcast() -> None:
    """Sending empty string to a group does not crash."""
    sessions = _sessions(3, prefix="em")
    wids = [s["session_id"] for s in sessions]

    async with live_server_with_bus(sessions, label="fanout_empty") as (hub, base_url):
        contexts = []
        for wid in wids:
            ctx = connect_async_ws(_ws_url(base_url, f"/ws/worker/{wid}/term"))
            ws = await ctx.__aenter__()
            contexts.append(ctx)
            await _drain_initial(ws)

        try:
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
                resp = await http.post(
                    "/api/fanout/groups",
                    json={
                        "name": "empty-input",
                        "worker_ids": wids,
                        "mode": "parallel",
                    },
                )
                assert resp.status_code == 200
                group_id = resp.json()["group_id"]

                # Don't emit any output; the quiesce timeout will fire
                resp = await http.post(
                    f"/api/fanout/groups/{group_id}/send",
                    json={
                        "data": "",
                        "quiesce_ms": 100,
                        "max_response_ms": 1000,
                    },
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["command"] == ""
                assert len(body["results"]) == 3
                # Workers are connected so send succeeds, but no output
                assert all(r["ok"] for r in body["results"])
        finally:
            for ctx in contexts:
                with contextlib.suppress(Exception):
                    await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 9. Rapid-fire broadcast — 5 sequential sends in quick succession
# ---------------------------------------------------------------------------


async def test_rapid_fire_broadcast() -> None:
    """5 sequential sends to the same group in quick succession all succeed."""
    sessions = _sessions(3, prefix="rf")
    wids = [s["session_id"] for s in sessions]

    async with live_server_with_bus(sessions, label="fanout_rapid") as (hub, base_url):
        contexts = []
        for wid in wids:
            ctx = connect_async_ws(_ws_url(base_url, f"/ws/worker/{wid}/term"))
            ws = await ctx.__aenter__()
            contexts.append(ctx)
            await _drain_initial(ws)

        try:
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=30.0) as http:
                resp = await http.post(
                    "/api/fanout/groups",
                    json={
                        "name": "rapid",
                        "worker_ids": wids,
                        "mode": "parallel",
                    },
                )
                assert resp.status_code == 200
                group_id = resp.json()["group_id"]

                results = []
                for i in range(5):

                    async def _emit(idx: int = i) -> None:
                        await asyncio.sleep(0.03)
                        for wid in wids:
                            await hub.append_event(wid, "term", {"data": f"result-{idx}\n"})

                    task = asyncio.create_task(_emit())
                    resp = await http.post(
                        f"/api/fanout/groups/{group_id}/send",
                        json={
                            "data": f"cmd-{i}\n",
                            "quiesce_ms": 100,
                            "max_response_ms": 2000,
                        },
                    )
                    await task
                    assert resp.status_code == 200
                    results.append(resp.json())

                # All 5 sends succeeded
                assert len(results) == 5
                for r in results:
                    assert len(r["results"]) == 3
                    assert r["failed_sessions"] == []
        finally:
            for ctx in contexts:
                with contextlib.suppress(Exception):
                    await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 10. Concurrent broadcasts to different groups
# ---------------------------------------------------------------------------


async def test_concurrent_broadcasts_different_groups() -> None:
    """Two groups broadcast simultaneously — results are correctly isolated."""
    sessions = _sessions(4, prefix="cg")
    wids = [s["session_id"] for s in sessions]
    group_a_wids = wids[:2]
    group_b_wids = wids[2:]

    async with live_server_with_bus(sessions, label="fanout_concurrent") as (hub, base_url):
        contexts = []
        for wid in wids:
            ctx = connect_async_ws(_ws_url(base_url, f"/ws/worker/{wid}/term"))
            ws = await ctx.__aenter__()
            contexts.append(ctx)
            await _drain_initial(ws)

        try:
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=15.0) as http:
                # Create two groups
                resp_a = await http.post(
                    "/api/fanout/groups",
                    json={
                        "name": "group-a",
                        "worker_ids": group_a_wids,
                        "mode": "parallel",
                    },
                )
                assert resp_a.status_code == 200
                gid_a = resp_a.json()["group_id"]

                resp_b = await http.post(
                    "/api/fanout/groups",
                    json={
                        "name": "group-b",
                        "worker_ids": group_b_wids,
                        "mode": "parallel",
                    },
                )
                assert resp_b.status_code == 200
                gid_b = resp_b.json()["group_id"]

                async def _emit_a() -> None:
                    await asyncio.sleep(0.05)
                    for wid in group_a_wids:
                        await hub.append_event(wid, "term", {"data": "alpha-output\n"})

                async def _emit_b() -> None:
                    await asyncio.sleep(0.05)
                    for wid in group_b_wids:
                        await hub.append_event(wid, "term", {"data": "beta-output\n"})

                async def _send_a() -> dict[str, Any]:
                    task = asyncio.create_task(_emit_a())
                    resp = await http.post(
                        f"/api/fanout/groups/{gid_a}/send",
                        json={
                            "data": "echo alpha\n",
                            "quiesce_ms": 200,
                            "max_response_ms": 3000,
                        },
                    )
                    await task
                    return resp.json()

                async def _send_b() -> dict[str, Any]:
                    task = asyncio.create_task(_emit_b())
                    resp = await http.post(
                        f"/api/fanout/groups/{gid_b}/send",
                        json={
                            "data": "echo beta\n",
                            "quiesce_ms": 200,
                            "max_response_ms": 3000,
                        },
                    )
                    await task
                    return resp.json()

                body_a, body_b = await asyncio.gather(_send_a(), _send_b())

                # Group A: 2 results, alpha output
                assert len(body_a["results"]) == 2
                assert all(r["ok"] for r in body_a["results"])
                assert body_a["group_id"] == gid_a

                # Group B: 2 results, beta output
                assert len(body_b["results"]) == 2
                assert all(r["ok"] for r in body_b["results"])
                assert body_b["group_id"] == gid_b
        finally:
            for ctx in contexts:
                with contextlib.suppress(Exception):
                    await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 11. Worker reconnect during fan-out
# ---------------------------------------------------------------------------


async def test_worker_reconnect_between_sends() -> None:
    """Worker disconnects and reconnects between fan-out sends — second send works."""
    sessions = _sessions(2, prefix="rc")
    wids = [s["session_id"] for s in sessions]

    async with live_server_with_bus(sessions, label="fanout_reconnect") as (hub, base_url):
        # Connect both workers
        ctx1a = connect_async_ws(_ws_url(base_url, f"/ws/worker/{wids[0]}/term"))
        ws1a = await ctx1a.__aenter__()
        await _drain_initial(ws1a)

        ctx2 = connect_async_ws(_ws_url(base_url, f"/ws/worker/{wids[1]}/term"))
        ws2 = await ctx2.__aenter__()
        await _drain_initial(ws2)

        try:
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=15.0) as http:
                resp = await http.post(
                    "/api/fanout/groups",
                    json={
                        "name": "reconnect",
                        "worker_ids": wids,
                        "mode": "parallel",
                    },
                )
                assert resp.status_code == 200
                group_id = resp.json()["group_id"]

                # First send — both connected
                async def _emit1() -> None:
                    await asyncio.sleep(0.03)
                    for wid in wids:
                        await hub.append_event(wid, "term", {"data": "first\n"})

                task = asyncio.create_task(_emit1())
                resp = await http.post(
                    f"/api/fanout/groups/{group_id}/send",
                    json={
                        "data": "cmd1\n",
                        "quiesce_ms": 200,
                        "max_response_ms": 3000,
                    },
                )
                await task
                assert resp.status_code == 200
                assert len(resp.json()["failed_sessions"]) == 0

                # Disconnect worker 1 and reconnect
                await ctx1a.__aexit__(None, None, None)
                await asyncio.sleep(0.1)
                ctx1b = connect_async_ws(_ws_url(base_url, f"/ws/worker/{wids[0]}/term"))
                ws1b = await ctx1b.__aenter__()
                await _drain_initial(ws1b)

                # Second send — both connected again
                async def _emit2() -> None:
                    await asyncio.sleep(0.03)
                    for wid in wids:
                        await hub.append_event(wid, "term", {"data": "second\n"})

                task = asyncio.create_task(_emit2())
                resp = await http.post(
                    f"/api/fanout/groups/{group_id}/send",
                    json={
                        "data": "cmd2\n",
                        "quiesce_ms": 200,
                        "max_response_ms": 3000,
                    },
                )
                await task
                assert resp.status_code == 200
                body = resp.json()
                assert len(body["failed_sessions"]) == 0
                assert all(r["ok"] for r in body["results"])

                await ctx1b.__aexit__(None, None, None)
        finally:
            with contextlib.suppress(Exception):
                await ctx2.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 12. Large output collection — workers send >10KB each
# ---------------------------------------------------------------------------


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
