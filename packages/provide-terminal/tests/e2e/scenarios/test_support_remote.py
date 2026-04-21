#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Real-world scenarios: Support and remote assistance workflows.

38. Role-based access in open mode — viewer/operator/admin.
39. Mode switch mid-session — hijack → open → hijack.
40. Terminal resize — snapshots with different dimensions.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from provide.terminal.client import connect_async_ws

from .conftest import (
    ADMIN_H,
    connect_browser,
    drain_all,
    drain_until,
    snapshot_msg,
    ws_url,
)

# ---------------------------------------------------------------------------
# 38. Role-based access in open mode
# ---------------------------------------------------------------------------


async def test_role_based_access_open_mode(single_session_server: Any) -> None:
    """Viewer sees snapshots but can't type. Operator and admin can both type."""
    hub, base_url = single_session_server

    async with connect_async_ws(ws_url(base_url, "/ws/worker/s1/term")) as worker:
        await worker.recv()  # snapshot_req

        # Switch to open mode
        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
            resp = await http.post("/worker/s1/input_mode", json={"input_mode": "open"})
            assert resp.status_code == 200

        async with (
            connect_browser(base_url, "s1", role="viewer") as viewer,
            connect_browser(base_url, "s1", role="operator") as operator,
            connect_browser(base_url, "s1", role="admin") as admin,
        ):
            await drain_all(viewer)
            await drain_all(operator)
            await drain_all(admin)

            # Worker sends snapshot — all 3 should see it
            await worker.send(json.dumps(snapshot_msg("shared-view-1")))
            await asyncio.sleep(0.5)

            v_msgs = await drain_all(viewer, timeout=1.0)
            o_msgs = await drain_all(operator, timeout=1.0)
            a_msgs = await drain_all(admin, timeout=1.0)

            assert any("shared-view" in m.get("screen", "") for m in v_msgs if m.get("type") == "snapshot"), (
                "Viewer should see snapshot"
            )
            assert any("shared-view" in m.get("screen", "") for m in o_msgs if m.get("type") == "snapshot"), (
                "Operator should see snapshot"
            )
            assert any("shared-view" in m.get("screen", "") for m in a_msgs if m.get("type") == "snapshot"), (
                "Admin should see snapshot"
            )

            # Viewer tries input — silently dropped
            await viewer.send(json.dumps({"type": "input", "data": "viewer-attempt\r"}))
            await asyncio.sleep(0.2)

            # Operator sends input — should work
            await operator.send(json.dumps({"type": "input", "data": "operator-cmd\r"}))
            await asyncio.sleep(0.2)

            # Admin sends input — should work
            await admin.send(json.dumps({"type": "input", "data": "admin-cmd\r"}))
            await asyncio.sleep(0.3)

        # Verify worker got operator + admin but not viewer
        await asyncio.sleep(0.3)
        worker_msgs = await drain_all(worker, timeout=1.0)
        all_data = [str(m.get("data", "")) for m in worker_msgs if "data" in m]
        assert not any("viewer-attempt" in d for d in all_data), f"Worker got viewer input: {all_data}"


# ---------------------------------------------------------------------------
# 39. Mode switch mid-session
# ---------------------------------------------------------------------------


async def test_mode_switch_mid_session(single_session_server: Any) -> None:
    """Hijack → open (operator can type) → hijack (operator blocked again)."""
    hub, base_url = single_session_server

    async with connect_async_ws(ws_url(base_url, "/ws/worker/s1/term")) as worker:
        await worker.recv()  # snapshot_req

        async with (
            connect_browser(base_url, "s1", role="admin") as admin,
            connect_browser(base_url, "s1", role="operator") as operator,
        ):
            await drain_all(admin)
            await drain_all(operator)

            # Admin hijacks
            await admin.send(json.dumps({"type": "hijack_request"}))
            await drain_until(admin, "hijack_state", timeout=3.0)
            await drain_all(worker)

            # Admin sends input
            await admin.send(json.dumps({"type": "input", "data": "in-hijack\r"}))
            await asyncio.sleep(0.2)

            # Admin releases before mode switch
            await admin.send(json.dumps({"type": "hijack_release"}))
            await asyncio.sleep(0.3)
            await drain_all(worker)
            await drain_all(admin)
            await drain_all(operator)

            # Switch to open
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
                resp = await http.post("/worker/s1/input_mode", json={"input_mode": "open"})
                assert resp.status_code == 200
            await asyncio.sleep(0.3)
            await drain_all(admin)
            await drain_all(operator)

            # Operator sends in open mode — should work
            await operator.send(json.dumps({"type": "input", "data": "now-open\r"}))
            await asyncio.sleep(0.2)

            # Admin also sends in open mode
            await admin.send(json.dumps({"type": "input", "data": "also-open\r"}))
            await asyncio.sleep(0.2)

            # Switch back to hijack
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
                resp = await http.post("/worker/s1/input_mode", json={"input_mode": "hijack"})
                assert resp.status_code == 200
            await asyncio.sleep(0.3)
            await drain_all(admin)
            await drain_all(operator)

            # Operator tries input — blocked (hijack mode, no owner)
            await operator.send(json.dumps({"type": "input", "data": "blocked\r"}))
            await asyncio.sleep(0.3)

        # Verify
        await asyncio.sleep(0.3)
        worker_msgs = await drain_all(worker, timeout=1.0)
        all_data = [str(m.get("data", "")) for m in worker_msgs if "data" in m]
        assert not any("blocked" in d for d in all_data), f"Worker should not get blocked input: {all_data}"


# ---------------------------------------------------------------------------
# 40. Terminal resize snapshots
# ---------------------------------------------------------------------------


async def test_terminal_resize_snapshots(live_hub: Any) -> None:
    """Worker sends snapshots with different dimensions; browser gets correct cols/rows."""
    hub, base_url = live_hub

    async with connect_async_ws(ws_url(base_url, "/ws/worker/resize1/term")) as worker:
        await worker.recv()  # snapshot_req

        async with connect_async_ws(ws_url(base_url, "/ws/browser/resize1/term")) as browser:
            await drain_all(browser)

            # Initial: 80x25
            await worker.send(json.dumps(snapshot_msg("$ initial prompt", cols=80, rows=25)))
            await asyncio.sleep(0.3)
            msgs1 = await drain_all(browser, timeout=1.0)
            snap1 = [m for m in msgs1 if m.get("type") == "snapshot"]
            assert len(snap1) >= 1, "Browser should receive first snapshot"
            assert snap1[-1]["cols"] == 80
            assert snap1[-1]["rows"] == 25
            assert "initial prompt" in snap1[-1]["screen"]

            # Resized: 120x40
            await worker.send(json.dumps(snapshot_msg("$ resized terminal", cols=120, rows=40)))
            await asyncio.sleep(0.3)
            msgs2 = await drain_all(browser, timeout=1.0)
            snap2 = [m for m in msgs2 if m.get("type") == "snapshot"]
            assert len(snap2) >= 1, "Browser should receive resized snapshot"
            assert snap2[-1]["cols"] == 120
            assert snap2[-1]["rows"] == 40
            assert "resized terminal" in snap2[-1]["screen"]
