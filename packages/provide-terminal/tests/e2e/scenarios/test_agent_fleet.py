#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Real-world scenarios: Agent fleet supervision workflows.

35. Human interrupts AI agent mid-output.
36. Multi-session dashboard — disconnect one session mid-stream.
37. Agent recovery — output resumes after supervisor releases.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from provide.terminal.client import connect_async_ws

from .conftest import (
    connect_browser,
    drain_all,
    drain_until,
    snapshot_msg,
    ws_url,
)


# ---------------------------------------------------------------------------
# 35. Human interrupts AI agent
# ---------------------------------------------------------------------------


async def test_human_interrupts_ai_agent(live_hub: Any) -> None:
    """Agent streams output; human hijacks, sends correction, releases; agent resumes."""
    hub, base_url = live_hub

    async with connect_async_ws(ws_url(base_url, "/ws/worker/agent1/term")) as worker:
        await worker.recv()  # snapshot_req

        # Agent sends rapid output
        for i in range(10):
            await worker.send(json.dumps(snapshot_msg(f"agent-output-{i}")))
        await asyncio.sleep(0.3)

        # Human connects and watches
        async with connect_async_ws(ws_url(base_url, "/ws/browser/agent1/term")) as human:
            initial_msgs = await drain_all(human, timeout=1.0)
            initial_snapshots = [m for m in initial_msgs if m.get("type") == "snapshot"]
            assert len(initial_snapshots) >= 1, "Human should see cached agent output"

            # Agent sends more while human watches
            for i in range(10, 15):
                await worker.send(json.dumps(snapshot_msg(f"agent-output-{i}")))
            await asyncio.sleep(0.3)

            pre_hijack_msgs = await drain_all(human, timeout=1.0)
            pre_snapshots = [m for m in pre_hijack_msgs if m.get("type") == "snapshot"]
            assert len(pre_snapshots) >= 1, "Human should see live agent output"

            # Human hijacks
            await human.send(json.dumps({"type": "hijack_request"}))
            await drain_until(human, "hijack_state", timeout=3.0)

            # Worker should have received pause
            worker_msgs = await drain_all(worker, timeout=1.0)
            pause_msgs = [m for m in worker_msgs if m.get("type") == "control" and m.get("action") == "pause"]
            assert len(pause_msgs) >= 1, "Worker should receive pause on hijack"

            # Human sends correction
            await human.send(json.dumps({"type": "input", "data": "STOP: use --dry-run\r"}))
            await asyncio.sleep(0.2)

            # Human releases
            await human.send(json.dumps({"type": "hijack_release"}))
            await asyncio.sleep(0.3)

            # Worker should have received resume
            worker_msgs2 = await drain_all(worker, timeout=1.0)
            resume_msgs = [m for m in worker_msgs2 if m.get("type") == "control" and m.get("action") == "resume"]
            assert len(resume_msgs) >= 1, "Worker should receive resume on release"

            # Agent resumes sending output
            for i in range(15, 20):
                await worker.send(json.dumps(snapshot_msg(f"agent-resumed-{i}")))
            await asyncio.sleep(0.5)

            post_msgs = await drain_all(human, timeout=2.0)
            post_snapshots = [m for m in post_msgs if m.get("type") == "snapshot"]
            assert len(post_snapshots) >= 3, (
                f"Human should receive post-release snapshots, got {len(post_snapshots)}"
            )


# ---------------------------------------------------------------------------
# 36. Multi-session dashboard
# ---------------------------------------------------------------------------


async def test_multi_session_dashboard_disconnect_one(three_session_server: Any) -> None:
    """Browser monitors 3 sessions; disconnecting one doesn't affect others."""
    hub, base_url = three_session_server

    async with (
        connect_async_ws(ws_url(base_url, "/ws/worker/s1/term")) as w1,
        connect_async_ws(ws_url(base_url, "/ws/worker/s2/term")) as w2,
        connect_async_ws(ws_url(base_url, "/ws/worker/s3/term")) as w3,
    ):
        await w1.recv()
        await w2.recv()
        await w3.recv()

        ctx_b1 = connect_browser(base_url, "s1", role="admin")
        ctx_b2 = connect_browser(base_url, "s2", role="admin")
        ctx_b3 = connect_browser(base_url, "s3", role="admin")

        b1 = await ctx_b1.__aenter__()
        b2 = await ctx_b2.__aenter__()
        b3 = await ctx_b3.__aenter__()

        try:
            await drain_all(b1)
            await drain_all(b2)
            await drain_all(b3)

            # Each worker sends unique snapshot
            await w1.send(json.dumps(snapshot_msg("session-1-output")))
            await w2.send(json.dumps(snapshot_msg("session-2-output")))
            await w3.send(json.dumps(snapshot_msg("session-3-output")))
            await asyncio.sleep(0.5)

            # Verify each browser got its own session's snapshot
            b1_msgs = await drain_all(b1, timeout=1.0)
            b2_msgs = await drain_all(b2, timeout=1.0)
            b3_msgs = await drain_all(b3, timeout=1.0)

            assert any("session-1" in m.get("screen", "") for m in b1_msgs if m.get("type") == "snapshot")
            assert any("session-2" in m.get("screen", "") for m in b2_msgs if m.get("type") == "snapshot")
            assert any("session-3" in m.get("screen", "") for m in b3_msgs if m.get("type") == "snapshot")

            # Disconnect browser for session 2
            await ctx_b2.__aexit__(None, None, None)
            b2 = None  # type: ignore[assignment]
            await asyncio.sleep(0.3)

            # Workers 1 and 3 send more
            await w1.send(json.dumps(snapshot_msg("s1-after-disconnect")))
            await w3.send(json.dumps(snapshot_msg("s3-after-disconnect")))
            await asyncio.sleep(0.5)

            b1_later = await drain_all(b1, timeout=1.0)
            b3_later = await drain_all(b3, timeout=1.0)

            assert any("s1-after" in m.get("screen", "") for m in b1_later if m.get("type") == "snapshot"), (
                f"Browser 1 should still receive: {b1_later}"
            )
            assert any("s3-after" in m.get("screen", "") for m in b3_later if m.get("type") == "snapshot"), (
                f"Browser 3 should still receive: {b3_later}"
            )

        finally:
            if b1:
                await ctx_b1.__aexit__(None, None, None)
            if b2:
                try:
                    await ctx_b2.__aexit__(None, None, None)
                except Exception:
                    pass
            if b3:
                await ctx_b3.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 37. Agent recovery after supervisor release
# ---------------------------------------------------------------------------


async def test_agent_recovery_output_resumes(live_hub: Any) -> None:
    """Output flow resumes immediately after hijack release — nothing frozen."""
    hub, base_url = live_hub

    async with connect_async_ws(ws_url(base_url, "/ws/worker/agent2/term")) as worker:
        await worker.recv()  # snapshot_req

        async with connect_async_ws(ws_url(base_url, "/ws/browser/agent2/term")) as browser:
            await drain_all(browser)

            # Pre-hijack: worker sends 3 snapshots
            for i in range(3):
                await worker.send(json.dumps(snapshot_msg(f"pre-{i}")))
            await asyncio.sleep(0.3)

            pre_msgs = await drain_all(browser, timeout=1.0)
            pre_snaps = [m for m in pre_msgs if m.get("type") == "snapshot"]
            assert len(pre_snaps) >= 1, "Browser should receive pre-hijack snapshots"

            # Hijack
            await browser.send(json.dumps({"type": "hijack_request"}))
            await drain_until(browser, "hijack_state", timeout=3.0)
            await drain_all(worker)

            # Send input
            await browser.send(json.dumps({"type": "input", "data": "fix config\r"}))
            await asyncio.sleep(0.2)

            # Release
            await browser.send(json.dumps({"type": "hijack_release"}))
            await asyncio.sleep(0.3)
            await drain_all(worker)
            await drain_all(browser)

            # Post-hijack: worker sends 3 more snapshots
            for i in range(3):
                await worker.send(json.dumps(snapshot_msg(f"post-{i}")))
            await asyncio.sleep(0.5)

            post_msgs = await drain_all(browser, timeout=2.0)
            post_snaps = [m for m in post_msgs if m.get("type") == "snapshot"]
            assert len(post_snaps) >= 2, (
                f"Browser should receive post-release snapshots, got {len(post_snaps)}"
            )
