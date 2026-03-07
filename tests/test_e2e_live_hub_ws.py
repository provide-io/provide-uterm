#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""End-to-end integration tests using real WebSocket connections against a live uvicorn TermHub.

These tests exercise the full network stack (HTTP upgrade, asyncio WS protocol)
that the FastAPI TestClient cannot reach.  Each test gets a fresh TermHub server
via the ``live_hub`` fixture from conftest.py.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import websockets

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ws_url(base_url: str, path: str) -> str:
    """Convert an http base URL to a ws:// URL with the given path."""
    return base_url.replace("http://", "ws://") + path


def _snapshot_msg(screen: str = "live test screen", prompt_id: str = "test_prompt") -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": "live-hash",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": prompt_id},
        "ts": time.time(),
    }


async def _drain(ws: Any, *, count: int = 1, timeout: float = 3.0) -> list[dict[str, Any]]:
    """Read up to *count* messages from *ws* within *timeout* seconds."""
    msgs: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(msgs) < count:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(0.3, remaining))
            msgs.append(json.loads(raw))
        except TimeoutError:
            continue
    return msgs


async def _drain_until(ws: Any, type_: str, timeout: float = 3.0) -> dict[str, Any] | None:
    """Drain messages until one with the given type arrives or timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
            msg = json.loads(raw)
            if msg.get("type") == type_:
                return msg
        except TimeoutError:
            continue
    return None


# ---------------------------------------------------------------------------
# Worker connect / disconnect
# ---------------------------------------------------------------------------


class TestWorkerConnect:
    async def test_worker_receives_snapshot_req_on_connect(self, live_hub: Any) -> None:
        """Hub sends snapshot_req immediately after worker connects."""
        _, base_url = live_hub
        async with websockets.connect(_ws_url(base_url, "/ws/worker/w1/term")) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            assert json.loads(msg)["type"] == "snapshot_req"

    async def test_worker_connected_broadcast_to_browsers(self, live_hub: Any) -> None:
        """Browser receives worker_connected when a worker joins."""
        _, base_url = live_hub
        async with websockets.connect(_ws_url(base_url, "/ws/browser/w2/term")) as browser:
            await _drain(browser, count=2)  # hello + hijack_state

            async with websockets.connect(_ws_url(base_url, "/ws/worker/w2/term")):
                msg = await _drain_until(browser, "worker_connected")
                assert msg is not None
                assert msg["worker_id"] == "w2"

    async def test_worker_disconnected_broadcast_to_browsers(self, live_hub: Any) -> None:
        """Browser receives worker_disconnected when the worker closes."""
        _, base_url = live_hub
        async with websockets.connect(_ws_url(base_url, "/ws/browser/w3/term")) as browser:
            await _drain(browser, count=2)

            async with websockets.connect(_ws_url(base_url, "/ws/worker/w3/term")):
                await _drain_until(browser, "worker_connected")

            # Worker exited; browser should learn about it
            msg = await _drain_until(browser, "worker_disconnected", timeout=3.0)
            assert msg is not None
            assert msg["worker_id"] == "w3"


# ---------------------------------------------------------------------------
# Terminal broadcast
# ---------------------------------------------------------------------------


class TestTermBroadcast:
    async def test_term_message_forwarded_to_browsers(self, live_hub: Any) -> None:
        """Term data from worker is broadcast to all connected browsers."""
        _, base_url = live_hub
        async with (
            websockets.connect(_ws_url(base_url, "/ws/browser/b1/term")) as browser,
            websockets.connect(_ws_url(base_url, "/ws/worker/b1/term")) as worker,
        ):
            await _drain(browser, count=3)  # hello, hijack_state, worker_connected
            await worker.recv()  # snapshot_req

            await worker.send(json.dumps({"type": "term", "data": "hello e2e", "ts": time.time()}))
            msg = await _drain_until(browser, "term")
            assert msg is not None
            assert msg["data"] == "hello e2e"

    async def test_snapshot_forwarded_to_browsers(self, live_hub: Any) -> None:
        """Snapshot from worker is broadcast to browsers."""
        _, base_url = live_hub
        async with (
            websockets.connect(_ws_url(base_url, "/ws/browser/b2/term")) as browser,
            websockets.connect(_ws_url(base_url, "/ws/worker/b2/term")) as worker,
        ):
            await _drain(browser, count=3)
            await worker.recv()  # snapshot_req

            await worker.send(json.dumps(_snapshot_msg("snapshot test")))
            msg = await _drain_until(browser, "snapshot")
            assert msg is not None
            assert msg["screen"] == "snapshot test"

    async def test_multiple_browsers_all_receive_broadcast(self, live_hub: Any) -> None:
        """Three browsers all receive the same term message from the worker."""
        _, base_url = live_hub
        b_url = _ws_url(base_url, "/ws/browser/b3/term")
        w_url = _ws_url(base_url, "/ws/worker/b3/term")

        async with (
            websockets.connect(b_url) as b1,
            websockets.connect(b_url) as b2,
            websockets.connect(b_url) as b3,
            websockets.connect(w_url) as worker,
        ):
            # Drain initial messages for all browsers
            for b in (b1, b2, b3):
                await _drain(b, count=3, timeout=3.0)
            await worker.recv()  # snapshot_req

            await worker.send(json.dumps({"type": "term", "data": "broadcast-test", "ts": time.time()}))

            results = await asyncio.gather(
                _drain_until(b1, "term"),
                _drain_until(b2, "term"),
                _drain_until(b3, "term"),
            )
            for msg in results:
                assert msg is not None
                assert msg["data"] == "broadcast-test"


# ---------------------------------------------------------------------------
# Browser hello / initial snapshot
# ---------------------------------------------------------------------------


class TestBrowserHello:
    async def test_hello_includes_worker_online_false_when_no_worker(self, live_hub: Any) -> None:
        """Hello message correctly reports worker_online=false when no worker is present."""
        _, base_url = live_hub
        async with websockets.connect(_ws_url(base_url, "/ws/browser/h1/term")) as browser:
            msgs = await _drain(browser, count=2)
            hello = next((m for m in msgs if m.get("type") == "hello"), None)
            assert hello is not None
            assert hello["worker_online"] is False

    async def test_hello_includes_worker_online_true_when_worker_present(self, live_hub: Any) -> None:
        """Hello message reports worker_online=true when a worker is already connected."""
        _, base_url = live_hub
        async with (
            websockets.connect(_ws_url(base_url, "/ws/worker/h2/term")),
            websockets.connect(_ws_url(base_url, "/ws/browser/h2/term")) as browser,
        ):
            msgs = await _drain(browser, count=2)
            hello = next((m for m in msgs if m.get("type") == "hello"), None)
            assert hello is not None
            assert hello["worker_online"] is True

    async def test_browser_receives_cached_snapshot_on_connect(self, live_hub: Any) -> None:
        """A browser connecting after a snapshot has been sent gets it immediately."""
        _, base_url = live_hub
        async with websockets.connect(_ws_url(base_url, "/ws/worker/h3/term")) as worker:
            await worker.recv()  # snapshot_req
            await worker.send(json.dumps(_snapshot_msg("cached screen content")))
            await asyncio.sleep(0.1)  # let hub store it

            async with websockets.connect(_ws_url(base_url, "/ws/browser/h3/term")) as browser:
                snapshot = await _drain_until(browser, "snapshot", timeout=2.0)
                assert snapshot is not None
                assert snapshot["screen"] == "cached screen content"


# ---------------------------------------------------------------------------
# REST hijack full cycle
# ---------------------------------------------------------------------------


class TestRestHijackCycle:
    async def test_acquire_pauses_worker(self, live_hub: Any) -> None:
        """REST hijack_acquire sends a pause control message to the worker."""
        _, base_url = live_hub
        async with websockets.connect(_ws_url(base_url, "/ws/worker/r1/term")) as worker:
            await worker.recv()  # snapshot_req

            async with httpx.AsyncClient(base_url=base_url) as http:
                r = await http.post("/worker/r1/hijack/acquire", json={"owner": "test", "lease_s": 60})
                assert r.status_code == 200

            ctrl = json.loads(await asyncio.wait_for(worker.recv(), timeout=2.0))
            assert ctrl["type"] == "control"
            assert ctrl["action"] == "pause"

    async def test_send_delivers_input_to_worker(self, live_hub: Any) -> None:
        """REST hijack_send delivers keystroke data to the worker after guard passes."""
        _, base_url = live_hub
        async with websockets.connect(_ws_url(base_url, "/ws/worker/r2/term")) as worker:
            await worker.recv()  # snapshot_req (sent on connect)

            async with httpx.AsyncClient(base_url=base_url) as http:
                r = await http.post("/worker/r2/hijack/acquire", json={"owner": "test", "lease_s": 60})
                hijack_id = r.json()["hijack_id"]
                await _drain_until(worker, "control")  # pause control

                # Send a snapshot so the guard check passes.
                await worker.send(json.dumps(_snapshot_msg()))
                await asyncio.sleep(0.1)

                r2 = await http.post(
                    f"/worker/r2/hijack/{hijack_id}/send",
                    json={"keys": "hello\r", "timeout_ms": 1000},
                )
                assert r2.status_code == 200

            # _wait_for_guard fires a snapshot_req before sending input; drain until input arrives.
            inp = await _drain_until(worker, "input")
            assert inp is not None
            assert inp["data"] == "hello\r"

    async def test_release_resumes_worker(self, live_hub: Any) -> None:
        """REST hijack_release sends a resume control message to the worker."""
        _, base_url = live_hub
        async with websockets.connect(_ws_url(base_url, "/ws/worker/r3/term")) as worker:
            await worker.recv()  # snapshot_req

            async with httpx.AsyncClient(base_url=base_url) as http:
                r = await http.post("/worker/r3/hijack/acquire", json={"owner": "test", "lease_s": 60})
                hijack_id = r.json()["hijack_id"]
                await worker.recv()  # pause

                r2 = await http.post(f"/worker/r3/hijack/{hijack_id}/release")
                assert r2.status_code == 200

            resume = json.loads(await asyncio.wait_for(worker.recv(), timeout=2.0))
            assert resume["type"] == "control"
            assert resume["action"] == "resume"

    async def test_step_sends_step_control_to_worker(self, live_hub: Any) -> None:
        """REST hijack_step sends a step control message to the worker."""
        _, base_url = live_hub
        async with websockets.connect(_ws_url(base_url, "/ws/worker/r4/term")) as worker:
            await worker.recv()

            async with httpx.AsyncClient(base_url=base_url) as http:
                r = await http.post("/worker/r4/hijack/acquire", json={"owner": "test", "lease_s": 60})
                hijack_id = r.json()["hijack_id"]
                await worker.recv()  # pause

                r2 = await http.post(f"/worker/r4/hijack/{hijack_id}/step")
                assert r2.status_code == 200

            ctrl = json.loads(await asyncio.wait_for(worker.recv(), timeout=2.0))
            assert ctrl["action"] == "step"

    async def test_heartbeat_extends_lease(self, live_hub: Any) -> None:
        """REST heartbeat returns an updated lease_expires_at."""
        _, base_url = live_hub
        async with websockets.connect(_ws_url(base_url, "/ws/worker/r5/term")) as worker:
            await worker.recv()

            async with httpx.AsyncClient(base_url=base_url) as http:
                r = await http.post("/worker/r5/hijack/acquire", json={"owner": "test", "lease_s": 60})
                data = r.json()
                hijack_id = data["hijack_id"]
                original_expires = data["lease_expires_at"]
                await worker.recv()  # pause

                r2 = await http.post(
                    f"/worker/r5/hijack/{hijack_id}/heartbeat",
                    json={"lease_s": 120},
                )
                assert r2.status_code == 200
                new_expires = r2.json()["lease_expires_at"]
                assert new_expires > original_expires


# ---------------------------------------------------------------------------
# WS hijack (dashboard) flow
# ---------------------------------------------------------------------------


class TestWsHijack:
    async def test_ws_hijack_request_pauses_worker(self, live_hub: Any) -> None:
        """WS hijack_request from browser triggers a pause on the worker."""
        _, base_url = live_hub
        async with (
            websockets.connect(_ws_url(base_url, "/ws/worker/ws1/term")) as worker,
            websockets.connect(_ws_url(base_url, "/ws/browser/ws1/term")) as browser,
        ):
            await _drain(browser, count=3)
            await _drain_until(worker, "snapshot_req")  # drain all initial snapshot_reqs

            await browser.send(json.dumps({"type": "hijack_request"}))

            # Hub may send another snapshot_req before pause; drain until control arrives.
            ctrl = await _drain_until(worker, "control")
            assert ctrl is not None
            assert ctrl["action"] == "pause"

            hijack_state = await _drain_until(browser, "hijack_state")
            assert hijack_state is not None
            assert hijack_state["hijacked"] is True
            assert hijack_state["owner"] == "me"

    async def test_ws_hijack_release_resumes_worker(self, live_hub: Any) -> None:
        """WS hijack_release from browser triggers a resume on the worker."""
        _, base_url = live_hub
        async with (
            websockets.connect(_ws_url(base_url, "/ws/worker/ws2/term")) as worker,
            websockets.connect(_ws_url(base_url, "/ws/browser/ws2/term")) as browser,
        ):
            await _drain(browser, count=3)
            await _drain_until(worker, "snapshot_req")  # drain initial snapshot_reqs

            await browser.send(json.dumps({"type": "hijack_request"}))
            await _drain_until(worker, "control")  # pause (skips any snapshot_req)
            await _drain_until(browser, "hijack_state")

            await browser.send(json.dumps({"type": "hijack_release"}))

            resume = await _drain_until(worker, "control")
            assert resume is not None
            assert resume["action"] == "resume"

    async def test_ws_input_forwarded_to_worker(self, live_hub: Any) -> None:
        """WS input message from browser owner is forwarded to the worker."""
        _, base_url = live_hub
        async with (
            websockets.connect(_ws_url(base_url, "/ws/worker/ws3/term")) as worker,
            websockets.connect(_ws_url(base_url, "/ws/browser/ws3/term")) as browser,
        ):
            await _drain(browser, count=3)
            await _drain_until(worker, "snapshot_req")  # drain initial snapshot_reqs

            await browser.send(json.dumps({"type": "hijack_request"}))
            await _drain_until(worker, "control")  # pause (skips any snapshot_req)
            await _drain_until(browser, "hijack_state")

            await browser.send(json.dumps({"type": "input", "data": "testkey"}))
            inp = await _drain_until(worker, "input")
            assert inp is not None
            assert inp["data"] == "testkey"

    async def test_second_browser_sees_hijacked_other(self, live_hub: Any) -> None:
        """A second browser connecting during an active WS hijack sees owner='other'."""
        _, base_url = live_hub
        async with (
            websockets.connect(_ws_url(base_url, "/ws/worker/ws4/term")) as worker,
            websockets.connect(_ws_url(base_url, "/ws/browser/ws4/term")) as b1,
        ):
            await _drain(b1, count=3)
            await _drain_until(worker, "snapshot_req")  # drain initial snapshot_reqs

            await b1.send(json.dumps({"type": "hijack_request"}))
            await _drain_until(worker, "control")  # pause (skips any snapshot_req)
            await _drain_until(b1, "hijack_state")

            async with websockets.connect(_ws_url(base_url, "/ws/browser/ws4/term")) as b2:
                msgs = await _drain(b2, count=3)
                hijack_state = next((m for m in msgs if m.get("type") == "hijack_state"), None)
                assert hijack_state is not None
                assert hijack_state["hijacked"] is True
                assert hijack_state["owner"] == "other"

    async def test_hijack_handoff_between_two_browsers(self, live_hub: Any) -> None:
        """After browser 1 releases, browser 2 can acquire hijack and browser 1 sees owner='other'."""
        _, base_url = live_hub
        async with (
            websockets.connect(_ws_url(base_url, "/ws/worker/ws5/term")) as worker,
            websockets.connect(_ws_url(base_url, "/ws/browser/ws5/term")) as b1,
            websockets.connect(_ws_url(base_url, "/ws/browser/ws5/term")) as b2,
        ):
            await _drain(b1, count=3)
            await _drain(b2, count=3)
            await _drain_until(worker, "snapshot_req")

            await b1.send(json.dumps({"type": "hijack_request"}))
            pause = await _drain_until(worker, "control")
            assert pause is not None
            assert pause["action"] == "pause"

            b1_state = await _drain_until(b1, "hijack_state")
            assert b1_state is not None
            assert b1_state["owner"] == "me"

            b2_state = await _drain_until(b2, "hijack_state")
            assert b2_state is not None
            assert b2_state["owner"] == "other"

            await b1.send(json.dumps({"type": "hijack_release"}))
            resume = await _drain_until(worker, "control")
            assert resume is not None
            assert resume["action"] == "resume"

            await _drain_until(b1, "hijack_state")
            await _drain_until(b2, "hijack_state")

            await b2.send(json.dumps({"type": "hijack_request"}))
            pause2 = await _drain_until(worker, "control")
            assert pause2 is not None
            assert pause2["action"] == "pause"

            b2_state2 = await _drain_until(b2, "hijack_state")
            assert b2_state2 is not None
            assert b2_state2["owner"] == "me"

            b1_state2 = await _drain_until(b1, "hijack_state")
            assert b1_state2 is not None
            assert b1_state2["owner"] == "other"
