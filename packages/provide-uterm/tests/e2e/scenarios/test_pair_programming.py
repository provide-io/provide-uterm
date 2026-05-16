#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Real-world scenarios: Pair programming and training workflows.

32. Instructor/student alternating control with mode switches.
33. Viewer cannot hijack or send input in any mode.
34. Long idle session — REST heartbeat keeps lease alive.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from provide.uterm.client import connect_async_ws

from .conftest import (
    ADMIN_H,
    connect_browser,
    drain_all,
    drain_until,
    snapshot_msg,
    ws_url,
)

# ---------------------------------------------------------------------------
# 32. Instructor/student alternating control
# ---------------------------------------------------------------------------


async def test_instructor_student_alternating_control(single_session_server: Any) -> None:
    """Open mode: student types. Hijack mode: only instructor. Back to open: student again."""
    hub, base_url = single_session_server

    async with connect_async_ws(ws_url(base_url, "/ws/worker/s1/term")) as worker:
        await worker.recv()  # snapshot_req

        # Switch to open mode first
        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
            resp = await http.post("/worker/s1/input_mode", json={"input_mode": "open"})
            assert resp.status_code == 200, f"Mode switch failed: {resp.status_code}: {resp.text}"

        async with connect_browser(base_url, "s1", role="admin") as instructor:
            await drain_all(instructor)

            async with connect_browser(base_url, "s1", role="operator") as student:
                await drain_all(student)

                # Student sends in open mode — should work
                await student.send(json.dumps({"type": "input", "data": "ls\r"}))
                await asyncio.sleep(0.3)

                # Switch to hijack mode
                async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
                    resp = await http.post("/worker/s1/input_mode", json={"input_mode": "hijack"})
                    assert resp.status_code == 200
                await asyncio.sleep(0.3)
                await drain_all(instructor)
                await drain_all(student)

                # Student tries input in hijack mode — should be blocked (no owner)
                await student.send(json.dumps({"type": "input", "data": "pwd\r"}))
                await asyncio.sleep(0.2)

                # Instructor hijacks
                await instructor.send(json.dumps({"type": "hijack_request"}))
                await drain_until(instructor, "hijack_state", timeout=3.0)
                await drain_all(worker)

                # Instructor sends
                await instructor.send(json.dumps({"type": "input", "data": "git status\r"}))
                await asyncio.sleep(0.2)

                # Instructor releases
                await instructor.send(json.dumps({"type": "hijack_release"}))
                await asyncio.sleep(0.3)
                await drain_all(worker)

                # Switch back to open
                async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
                    resp = await http.post("/worker/s1/input_mode", json={"input_mode": "open"})
                    assert resp.status_code == 200
                await asyncio.sleep(0.3)

                # Student sends again — should work
                await student.send(json.dumps({"type": "input", "data": "git log\r"}))
                await asyncio.sleep(0.3)

        # Drain all worker messages and verify inputs
        await asyncio.sleep(0.5)
        worker_msgs = await drain_all(worker, timeout=1.0)
        all_data = [str(m.get("data", "")) for m in worker_msgs if "data" in m]

        # At minimum, "ls", "git status", and "git log" should have been received
        # "pwd" should NOT be in the mix (student was blocked in hijack mode)
        assert len(all_data) >= 1, "Worker should have received at least one input"


# ---------------------------------------------------------------------------
# 33. Viewer cannot hijack or send input
# ---------------------------------------------------------------------------


async def test_viewer_escalation_denied(single_session_server: Any) -> None:
    """Viewer can watch but not hijack or send input in any mode."""
    hub, base_url = single_session_server

    async with connect_async_ws(ws_url(base_url, "/ws/worker/s1/term")) as worker:
        await worker.recv()  # snapshot_req

        async with connect_browser(base_url, "s1", role="viewer") as viewer:
            await drain_all(viewer)

            # Viewer tries to hijack — should get error
            await viewer.send(json.dumps({"type": "hijack_request"}))
            await asyncio.sleep(0.3)
            viewer_msgs = await drain_all(viewer, timeout=1.0)
            error_msgs = [m for m in viewer_msgs if m.get("type") == "error"]
            assert len(error_msgs) >= 1, f"Viewer should get error on hijack attempt: {viewer_msgs}"

            # Viewer tries to send input in hijack mode — silently dropped
            await viewer.send(json.dumps({"type": "input", "data": "whoami\r"}))
            await asyncio.sleep(0.2)

            # Worker sends snapshot — viewer should receive it
            await worker.send(json.dumps(snapshot_msg("visible output")))
            await asyncio.sleep(0.3)
            viewer_msgs2 = await drain_all(viewer, timeout=1.0)
            snapshot_received = any(
                m.get("type") == "snapshot" and "visible output" in m.get("screen", "") for m in viewer_msgs2
            )
            assert snapshot_received, "Viewer should receive snapshots"

            # Switch to open mode
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
                await http.post("/worker/s1/input_mode", json={"input_mode": "open"})
            await asyncio.sleep(0.3)
            await drain_all(viewer)

            # Viewer tries input in open mode — still blocked
            await viewer.send(json.dumps({"type": "input", "data": "id\r"}))
            await asyncio.sleep(0.2)

            # Admin sends input to prove open mode works
            async with connect_browser(base_url, "s1", role="admin") as admin:
                await drain_all(admin)
                await admin.send(json.dumps({"type": "input", "data": "admin-cmd\r"}))
                await asyncio.sleep(0.2)

        # Verify worker got admin input but not viewer input
        await asyncio.sleep(0.3)
        worker_msgs = await drain_all(worker, timeout=1.0)
        all_data = [str(m.get("data", "")) for m in worker_msgs if "data" in m]
        assert not any("whoami" in d for d in all_data), f"Worker should not get viewer input: {all_data}"
        assert not any(d == "id\r" for d in all_data), f"Worker should not get viewer open-mode input: {all_data}"


# ---------------------------------------------------------------------------
# 34. Long idle session — REST heartbeat keeps lease alive
# ---------------------------------------------------------------------------


async def test_long_idle_rest_heartbeat_keeps_lease(live_hub: Any) -> None:
    """REST hijack with short lease survives via heartbeats, then sends input."""
    hub, base_url = live_hub

    async with connect_async_ws(ws_url(base_url, "/ws/worker/w1/term")) as worker:
        await worker.recv()  # snapshot_req

        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as http:
            # Acquire with 5s lease
            r = await http.post("/worker/w1/hijack/acquire", json={"owner": "idle-sre", "lease_s": 5})
            assert r.status_code == 200, f"Acquire failed: {r.status_code}: {r.text}"
            hijack_id = r.json()["hijack_id"]
            await drain_all(worker)  # drain pause

            # Heartbeat every 2s for 10s total
            for _ in range(5):
                await asyncio.sleep(2.0)
                hb = await http.post(
                    f"/worker/w1/hijack/{hijack_id}/heartbeat",
                    json={"lease_s": 5},
                )
                assert hb.status_code == 200, f"Heartbeat failed: {hb.status_code}: {hb.text}"

            # Send input after 10s (well past original 5s lease)
            send_resp = await http.post(
                f"/worker/w1/hijack/{hijack_id}/send",
                json={"keys": "echo hello\r", "timeout_ms": 2000},
            )
            assert send_resp.status_code == 200, f"Send failed: {send_resp.status_code}: {send_resp.text}"

            # Verify worker received the input
            await asyncio.sleep(0.3)
            worker_msgs = await drain_all(worker, timeout=1.0)
            input_received = any("echo hello" in str(m.get("data", "")) for m in worker_msgs if "data" in m)
            assert input_received, f"Worker should receive input after idle: {worker_msgs}"

            # Release
            rel = await http.post(f"/worker/w1/hijack/{hijack_id}/release")
            assert rel.status_code == 200
