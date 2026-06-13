#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E: Race-condition and contention scenarios for multi-browser sessions.

Scenarios
---------
5. Thundering herd — 5 browsers simultaneously request hijack; exactly 1 wins.
6. Mode switch during active hijack — REST PATCH forces release mid-hijack.
7. Resume token reclaim vs. competing browser — only one can own the hijack.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from provide.uterm.client import connect_async_ws

from provide.uterm.server.bridge.hub import InMemoryResumeStore, TermHub

from .conftest import (
    ADMIN_H,
    connect_browser,
    drain_all,
    drain_until,
    ws_url,
)

# ---------------------------------------------------------------------------
# 5. Thundering herd — 5 simultaneous hijack requests
# ---------------------------------------------------------------------------


async def test_thundering_herd_5_browsers_simultaneous_hijack(live_server: Any) -> None:
    """Five browsers fire hijack_request at once; exactly one wins ownership."""
    hub, base_url = live_server

    async with connect_async_ws(ws_url(base_url, "/ws/worker/herd1/term")) as worker:
        await worker.recv()  # snapshot_req

        browsers = []
        try:
            for _ in range(5):
                ctx = connect_browser(base_url, "herd1", role="admin")
                ws = await ctx.__aenter__()
                browsers.append((ws, ctx))
                await drain_all(ws)

            # All 5 send hijack_request simultaneously
            await asyncio.gather(*(ws.send_json({"type": "hijack_request"}) for ws, _ in browsers))
            await asyncio.sleep(0.5)

            # Drain all browsers and count who got owner=me
            me_count = 0
            for ws, _ in browsers:
                msgs = await drain_all(ws, timeout=1.0)
                for msg in msgs:
                    if msg.get("type") == "hijack_state" and (msg.get("hijacked_by_me") or msg.get("owner") == "me"):
                        me_count += 1

            assert me_count == 1, f"Exactly 1 browser should own hijack, got {me_count}"

            # Worker receives pause controls — the hub sends pause before
            # acquiring the lock, so multiple pauses are expected. The key
            # invariant is that exactly one browser owns it.
            worker_msgs = await drain_all(worker, timeout=1.0)
            pause_count = sum(1 for m in worker_msgs if m.get("type") == "control" and m.get("action") == "pause")
            assert pause_count >= 1, "Worker should receive at least one pause"
        finally:
            for _ws, ctx in reversed(browsers):
                await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 6. Mode switch during active hijack
# ---------------------------------------------------------------------------


async def test_mode_switch_during_active_hijack() -> None:
    """REST PATCH to open mode force-releases an active WS hijack."""
    from .._live_server import live_server_with_bus  # noqa: TID252

    sessions = [
        {"session_id": "mode1", "display_name": "Mode", "connector_type": "shell", "auto_start": False},
    ]
    async with (
        live_server_with_bus(sessions, label="mode_switch") as (hub, base_url),
        connect_async_ws(ws_url(base_url, "/ws/worker/mode1/term")) as worker,
    ):
        await worker.recv()  # snapshot_req

        async with connect_browser(base_url, "mode1", role="admin") as b1:
            await drain_all(b1)

            # B1 acquires hijack
            await b1.send_json({"type": "hijack_request"})
            hijack_msg = await drain_until(b1, "hijack_state", timeout=3.0)
            assert hijack_msg is not None
            # Drain worker pause
            await drain_until(worker, "control", timeout=3.0)

            # REST PATCH: switch to open mode
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
                resp = await http.patch(
                    "/api/sessions/mode1",
                    json={"input_mode": "open"},
                )
                assert resp.status_code == 200, f"PATCH failed: {resp.status_code}: {resp.text}"

            # Give the server time to process mode switch
            await asyncio.sleep(0.5)

            # Verify via REST that the mode changed
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http2:
                status_resp = await http2.get("/api/sessions/mode1")
                assert status_resp.status_code == 200
                session_data = status_resp.json()
                actual_mode = session_data.get("input_mode", session_data.get("definition", {}).get("input_mode"))
                assert actual_mode == "open", f"Session mode should be 'open' after PATCH, got {actual_mode}"

            # The mode is now "open" — verify a second acquire would fail
            # (open mode doesn't allow hijack)
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http3:
                acq_resp = await http3.post(
                    "/worker/mode1/hijack/acquire",
                    json={"owner": "should-fail", "lease_s": 10},
                )
                # In open mode, acquire should be rejected (409 or similar)
                assert acq_resp.status_code != 200, (
                    f"Hijack acquire should fail in open mode, got {acq_resp.status_code}"
                )


# ---------------------------------------------------------------------------
# 7. Resume token reclaim vs. competing browser
# ---------------------------------------------------------------------------


async def test_resume_token_reclaim_vs_competing_browser() -> None:
    """Disconnected browser with resume token competes with a new browser for hijack."""
    hub = TermHub(
        resolve_browser_role=lambda _ws, _wid: "admin",
        resume_store=InMemoryResumeStore(),
    )
    app = FastAPI()
    app.include_router(hub.create_router())

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not server.started:
            if loop.time() > deadline:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=2.0)
                raise RuntimeError("resume test: uvicorn startup timeout")
            await asyncio.sleep(0.05)

        port: int = server.servers[0].sockets[0].getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        ws_base = base_url.replace("http://", "ws://")

        async with connect_async_ws(f"{ws_base}/ws/worker/resume1/term") as worker:
            await worker.recv()  # snapshot_req

            # B1 connects, gets resume token, acquires hijack
            async with connect_async_ws(f"{ws_base}/ws/browser/resume1/term") as b1:
                b1_msgs = await drain_all(b1, timeout=1.0)
                resume_token = None
                for msg in b1_msgs:
                    if msg.get("type") == "hello":
                        resume_token = msg.get("resume_token")
                        break

                await b1.send_json({"type": "hijack_request"})
                await drain_until(b1, "hijack_state", timeout=3.0)
                await drain_all(worker)  # drain pause

            # B1 disconnected — cleanup runs
            await asyncio.sleep(0.3)

            # B2 connects and tries to acquire
            async with connect_async_ws(f"{ws_base}/ws/browser/resume1/term") as b2:
                await drain_all(b2)
                await b2.send_json({"type": "hijack_request"})
                await asyncio.sleep(0.3)

                b2_msgs = await drain_all(b2, timeout=1.0)
                b2_owns = any(
                    m.get("type") == "hijack_state" and (m.get("hijacked_by_me") or m.get("owner") == "me")
                    for m in b2_msgs
                )

                # If B1 had a resume token, B1 reconnecting with it could reclaim.
                # But since B1 already disconnected and cleanup ran, B2 should win.
                # The key invariant: only one browser owns it at any time.
                if resume_token:
                    # Try reconnecting B1 with resume token
                    async with connect_async_ws(
                        f"{ws_base}/ws/browser/resume1/term?resume_token={resume_token}"
                    ) as b1_new:
                        b1_new_msgs = await drain_all(b1_new, timeout=1.0)
                        b1_new_owns = any(
                            m.get("type") == "hijack_state" and (m.get("hijacked_by_me") or m.get("owner") == "me")
                            for m in b1_new_msgs
                        )
                        # At most one can own it
                        assert not (b2_owns and b1_new_owns), (
                            "Both browsers claim ownership — resume/acquire race failed"
                        )
                else:
                    # No resume token — B2 should just win
                    assert b2_owns, "B2 should own hijack when no resume token exists"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)
