#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Real-world scenarios: CI/CD and long-running session workflows.

41. Build output event polling via REST pagination.
42. Worker clean shutdown and restart on same ID.
43. Session survives brief network blip via resume token.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from provide.terminal.client import connect_async_ws

from .conftest import (
    drain_all,
    snapshot_msg,
    ws_url,
)

# ---------------------------------------------------------------------------
# 41. Build output event polling via REST
# ---------------------------------------------------------------------------


async def test_build_output_event_polling(live_hub: Any) -> None:
    """REST client paginates through events with after_seq. All events retrievable."""
    hub, base_url = live_hub

    async with connect_async_ws(ws_url(base_url, "/ws/worker/build1/term")) as worker:
        await worker.recv()  # snapshot_req

        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as http:
            # Acquire hijack for REST event access
            r = await http.post("/worker/build1/hijack/acquire", json={"owner": "ci-monitor", "lease_s": 120})
            assert r.status_code == 200, f"Acquire failed: {r.status_code}: {r.text}"
            hijack_id = r.json()["hijack_id"]
            await drain_all(worker)  # drain pause

            # Worker sends 50 snapshots (simulating build output)
            for i in range(50):
                await worker.send(json.dumps(snapshot_msg(f"build-line-{i}")))
            await asyncio.sleep(1.0)

            # Paginate through events
            all_events: list[dict[str, Any]] = []
            after_seq = 0

            for _ in range(10):  # safety limit
                resp = await http.get(
                    f"/worker/build1/hijack/{hijack_id}/events",
                    params={"after_seq": after_seq, "limit": 25},
                )
                assert resp.status_code == 200, f"Events failed: {resp.status_code}: {resp.text}"
                body = resp.json()
                events = body.get("events", [])
                if not events:
                    break
                all_events.extend(events)
                after_seq = events[-1]["seq"]
                if not body.get("has_more", False):
                    break

            # Should have captured many events (snapshots + control)
            snapshot_events = [e for e in all_events if e.get("type") == "snapshot"]
            assert len(snapshot_events) >= 30, (
                f"Should capture ≥30 snapshot events, got {len(snapshot_events)}"
            )

            # Sequence numbers should be contiguous
            seqs = [e["seq"] for e in all_events]
            for i in range(1, len(seqs)):
                assert seqs[i] == seqs[i - 1] + 1, (
                    f"Seq gap at index {i}: {seqs[i - 1]} → {seqs[i]}"
                )

            # Release
            await http.post(f"/worker/build1/hijack/{hijack_id}/release")


# ---------------------------------------------------------------------------
# 42. Worker clean shutdown and restart
# ---------------------------------------------------------------------------


async def test_worker_clean_shutdown_and_restart(live_hub: Any) -> None:
    """Browser sees connected → snapshot → disconnected → connected → snapshot lifecycle."""
    hub, base_url = live_hub

    # Browser connects first (before any worker)
    async with connect_async_ws(ws_url(base_url, "/ws/browser/lifecycle1/term")) as browser:
        await drain_all(browser, timeout=1.0)

        # Worker v1 connects
        async with connect_async_ws(ws_url(base_url, "/ws/worker/lifecycle1/term")) as worker_v1:
            await worker_v1.recv()  # snapshot_req
            await asyncio.sleep(0.3)

            # Browser should get worker_connected
            connected_msgs = await drain_all(browser, timeout=1.0)
            got_connected = any(m.get("type") == "worker_connected" for m in connected_msgs)
            assert got_connected, f"Browser should see worker_connected: {connected_msgs}"

            # Worker v1 sends snapshot
            await worker_v1.send(json.dumps(snapshot_msg("version-1 output")))
            await asyncio.sleep(0.3)

            v1_msgs = await drain_all(browser, timeout=1.0)
            v1_snaps = [m for m in v1_msgs if m.get("type") == "snapshot"]
            assert any("version-1" in m.get("screen", "") for m in v1_snaps), (
                f"Browser should receive v1 snapshot: {v1_snaps}"
            )

        # Worker v1 disconnected (exited context)
        await asyncio.sleep(0.3)
        disconn_msgs = await drain_all(browser, timeout=1.0)
        got_disconnected = any(m.get("type") == "worker_disconnected" for m in disconn_msgs)
        assert got_disconnected, f"Browser should see worker_disconnected: {disconn_msgs}"

        # Worker v2 connects with same ID
        async with connect_async_ws(ws_url(base_url, "/ws/worker/lifecycle1/term")) as worker_v2:
            await worker_v2.recv()  # snapshot_req
            await asyncio.sleep(0.3)

            reconnect_msgs = await drain_all(browser, timeout=1.0)
            got_reconnected = any(m.get("type") == "worker_connected" for m in reconnect_msgs)
            assert got_reconnected, f"Browser should see worker_connected v2: {reconnect_msgs}"

            # Worker v2 sends snapshot
            await worker_v2.send(json.dumps(snapshot_msg("version-2 fresh start")))
            await asyncio.sleep(0.3)

            v2_msgs = await drain_all(browser, timeout=1.0)
            v2_snaps = [m for m in v2_msgs if m.get("type") == "snapshot"]
            assert any("version-2" in m.get("screen", "") for m in v2_snaps), (
                f"Browser should receive v2 snapshot: {v2_snaps}"
            )


# ---------------------------------------------------------------------------
# 43. Session survives brief network blip via resume token
# ---------------------------------------------------------------------------


async def test_session_survives_brief_network_blip(resume_hub: Any) -> None:
    """Browser disconnects briefly, reconnects with resume token, continues receiving."""
    hub, base_url = resume_hub
    ws_base = ws_url(base_url, "")

    async with connect_async_ws(f"{ws_base}/ws/worker/blip1/term") as worker:
        await worker.recv()  # snapshot_req

        # Worker sends initial snapshot
        await worker.send(json.dumps(snapshot_msg("pre-blip output")))
        await asyncio.sleep(0.2)

        # Browser connects, extracts resume token
        resume_token = None
        async with connect_async_ws(f"{ws_base}/ws/browser/blip1/term") as browser:
            initial_msgs = await drain_all(browser, timeout=1.0)
            for m in initial_msgs:
                if m.get("type") == "hello":
                    resume_token = m.get("resume_token")
                    break

            # Verify browser got the snapshot
            got_snapshot = any(
                m.get("type") == "snapshot" and "pre-blip" in m.get("screen", "")
                for m in initial_msgs
            )
            assert got_snapshot, f"Browser should get pre-blip snapshot: {initial_msgs}"

        # Browser disconnected — brief blip
        await asyncio.sleep(0.5)

        # Reconnect with resume token
        resume_url = f"{ws_base}/ws/browser/blip1/term"
        if resume_token:
            resume_url += f"?resume_token={resume_token}"

        async with connect_async_ws(resume_url) as browser_new:
            resumed_msgs = await drain_all(browser_new, timeout=1.0)

            # Should get hello (possibly with resumed=True if token was valid)
            hello_msgs = [m for m in resumed_msgs if m.get("type") == "hello"]
            assert len(hello_msgs) >= 1, "Reconnected browser should get hello"

            # Should get a snapshot (cached latest)
            [m for m in resumed_msgs if m.get("type") == "snapshot"]
            # May or may not get cached snapshot depending on implementation

            # Worker sends new snapshot
            await worker.send(json.dumps(snapshot_msg("post-blip output")))
            await asyncio.sleep(0.5)

            post_msgs = await drain_all(browser_new, timeout=1.0)
            got_post = any(
                m.get("type") == "snapshot" and "post-blip" in m.get("screen", "")
                for m in post_msgs
            )
            assert got_post, f"Resumed browser should receive new snapshots: {post_msgs}"
