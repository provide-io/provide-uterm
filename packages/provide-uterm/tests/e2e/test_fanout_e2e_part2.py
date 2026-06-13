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

from ._live_server import live_server_with_bus
from .test_fanout_e2e_part1 import _drain_initial, _sessions, _ws_url

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}


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
