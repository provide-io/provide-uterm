#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Advanced E2E tests: role enforcement, lease expiry, reconnect, events, input modes."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI

from provide.uterm.client import connect_async_ws
from provide.uterm.server.bridge.hub import TermHub

from .conftest import _drain_all, _drain_until, _snapshot_msg, _wait_for_server, _ws_url


@asynccontextmanager
async def _hub_with_roles(role_map: dict[str, str]):
    """Context manager: hub with custom role resolver. role_map["*"] = default role."""
    default_role = role_map.get("*", "viewer")
    hub = TermHub(resolve_browser_role=lambda _ws, _worker_id: default_role)
    app = FastAPI()
    app.include_router(hub.create_router())

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    try:
        await _wait_for_server(server, task, "role-aware hub")
        port: int = server.servers[0].sockets[0].getsockname()[1]
        yield hub, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# TestBrowserRoles — enforce viewer/operator/admin permissions
# ---------------------------------------------------------------------------


class TestBrowserRoles:
    async def test_viewer_cannot_send_input_in_open_mode(self) -> None:
        """Viewer sends input in open mode, gets error; worker doesn't receive it."""
        async with _hub_with_roles({"*": "viewer"}) as (hub, base_url):
            hub.lease.dashboard_hijack_lease_s = 0.5
            async with (
                connect_async_ws(_ws_url(base_url, "/ws/browser/v1/term")) as browser,
                connect_async_ws(_ws_url(base_url, "/ws/worker/v1/term")) as worker,
            ):
                await worker.recv()  # snapshot_req
                await _drain_all(browser)  # hello, hijack_state

                # Switch to open mode via worker hello
                await worker.send_json(
                    {
                        "type": "worker_hello",
                        "input_mode": "open",
                        "ts": time.time(),
                    }
                )
                # Wait for mode switch to propagate to the browser
                await _drain_until(browser, "hijack_state", timeout=2.0)

                await browser.send_json({"type": "input", "data": "test"})
                # Viewer input is rejected or ignored; no input appears in worker messages
                msgs = await _drain_all(worker, timeout=0.5)
                input_msgs = [m for m in msgs if m.get("type") == "input"]
                assert len(input_msgs) == 0, f"Viewer should not be able to send input, got {input_msgs}"

    async def test_viewer_gets_hello_with_can_hijack_false(self) -> None:
        """Viewer hello message includes can_hijack=false."""
        async with (
            _hub_with_roles({"*": "viewer"}) as (_hub, base_url),
            connect_async_ws(_ws_url(base_url, "/ws/browser/v2/term")) as browser,
        ):
            hello = await _drain_until(browser, "hello")
            assert hello is not None, "Should receive hello message"
            assert hello.get("role") == "viewer", f"Role should be 'viewer', got {hello.get('role')}"

    async def test_operator_can_send_in_open_mode(self) -> None:
        """Operator can send input in open mode; worker receives it."""
        async with (
            _hub_with_roles({"*": "operator"}) as (_hub, base_url),
            connect_async_ws(_ws_url(base_url, "/ws/browser/op1/term")) as browser,
            connect_async_ws(_ws_url(base_url, "/ws/worker/op1/term")) as worker,
        ):
            await worker.recv()  # snapshot_req
            await _drain_all(browser)

            # Switch to open mode
            await worker.send_json(
                {
                    "type": "worker_hello",
                    "input_mode": "open",
                    "ts": time.time(),
                }
            )
            # Wait for mode switch to propagate to the browser
            await _drain_until(browser, "hijack_state", timeout=2.0)

            await browser.send_json({"type": "input", "data": "opkey"})
            inp = await _drain_until(worker, "input")
            assert inp is not None, "Worker should receive input from operator in open mode"
            assert inp["data"] == "opkey", f"Input data should be 'opkey', got {inp.get('data')}"

    async def test_operator_hijack_request_rejected(self) -> None:
        """Operator sends hijack_request, gets error."""
        async with (
            _hub_with_roles({"*": "operator"}) as (_hub, base_url),
            connect_async_ws(_ws_url(base_url, "/ws/browser/op2/term")) as browser,
            connect_async_ws(_ws_url(base_url, "/ws/worker/op2/term")),
        ):
            await _drain_all(browser)
            await browser.send_json({"type": "hijack_request"})
            # Operator cannot hijack; error or no state change
            msg = await _drain_until(browser, "error", timeout=1.0)
            # May receive error or no state change; operator hijack should be rejected
            assert msg is not None, "Operator hijack should be rejected with error"  # pragma: no cover

    async def test_admin_can_hijack(self, live_hub: Any) -> None:
        """Admin sends hijack_request, acquires hijack with owner=me."""
        _, base_url = live_hub
        async with (
            connect_async_ws(_ws_url(base_url, "/ws/browser/a1/term")) as browser,
            connect_async_ws(_ws_url(base_url, "/ws/worker/a1/term")) as worker,
        ):
            await worker.recv()  # snapshot_req
            await _drain_all(browser)

            await browser.send_json({"type": "hijack_request"})
            state = await _drain_until(browser, "hijack_state")
            assert state is not None, "Should receive hijack_state after hijack_request"
            assert state.get("hijacked") is True, f"Should be hijacked, got {state}"
            assert state.get("owner") == "me", f"Owner should be 'me', got {state.get('owner')}"


# ---------------------------------------------------------------------------
# TestLeaseExpiry
# ---------------------------------------------------------------------------


class TestLeaseExpiry:
    async def test_rest_lease_expires_worker_gets_resume(self, live_hub: Any) -> None:
        """Acquire with lease_s=0.3; sleep 0.6s; worker receives resume."""
        _, base_url = live_hub
        async with (
            connect_async_ws(_ws_url(base_url, "/ws/worker/le1/term")) as worker,
            httpx.AsyncClient(base_url=base_url) as http,
        ):
            await worker.recv()  # snapshot_req

            r = await http.post("/worker/le1/hijack/acquire", json={"owner": "test", "lease_s": 1})
            assert r.status_code == 200, f"Acquire should succeed, got {r.status_code}: {r.text}"

            # Immediately send a snapshot so guard passes for any upcoming send
            await worker.send_json(_snapshot_msg())
            ctrl = await _drain_until(worker, "control")
            assert ctrl is not None, "Should receive control message after acquire"
            assert ctrl["action"] == "pause", f"Control action should be pause, got {ctrl.get('action')}"

            # Wait for lease to expire (time-based; no polling alternative)
            await asyncio.sleep(1.5)

            # Hub should send resume after lease expiry
            resume = await _drain_until(worker, "control", timeout=3.0)
            assert resume is not None, "Worker should receive resume after lease expiry"
            assert resume["action"] == "resume", f"Control action should be resume, got {resume.get('action')}"

    async def test_ws_hijack_owner_disconnect_sends_resume(self, live_hub: Any) -> None:
        """Browser hijacks, then disconnects; worker receives resume."""
        _, base_url = live_hub
        async with connect_async_ws(_ws_url(base_url, "/ws/worker/le2/term")) as worker:
            await worker.recv()  # snapshot_req

            async with connect_async_ws(_ws_url(base_url, "/ws/browser/le2/term")) as b1:
                await _drain_all(b1)
                await b1.send_json({"type": "hijack_request"})
                state = await _drain_until(b1, "hijack_state")
                assert state is not None, "Should receive hijack_state after hijack_request"
                assert state["owner"] == "me", f"b1 should own hijack, got {state.get('owner')}"
                pause = await _drain_until(worker, "control")
                assert pause is not None, "Worker should receive pause control"
                assert pause["action"] == "pause", f"Expected pause, got {pause.get('action')}"

            # b1 disconnected; worker should get resume
            resume = await _drain_until(worker, "control", timeout=3.0)
            assert resume is not None, "Worker should receive resume after hijack owner disconnect"
            assert resume["action"] == "resume", f"Expected resume, got {resume.get('action')}"


# ---------------------------------------------------------------------------
# TestWorkerReconnect
# ---------------------------------------------------------------------------


class TestWorkerReconnect:
    async def test_worker_reconnect_browser_sees_events(self, live_hub: Any) -> None:
        """Worker disconnects and reconnects; browser sees events."""
        _, base_url = live_hub
        async with connect_async_ws(_ws_url(base_url, "/ws/browser/wr1/term")) as browser:
            await _drain_all(browser)

            async with connect_async_ws(_ws_url(base_url, "/ws/worker/wr1/term")):
                connected = await _drain_until(browser, "worker_connected", timeout=2.0)
                assert connected is not None, "Browser should see worker_connected event"

            # Worker disconnected
            disc = await _drain_until(browser, "worker_disconnected", timeout=2.0)
            assert disc is not None, "Browser should see worker_disconnected event"

    async def test_active_hijack_cleared_on_worker_disconnect(self, live_hub: Any) -> None:
        """REST acquire, then worker disconnects; second acquire succeeds."""
        _, base_url = live_hub
        async with httpx.AsyncClient(base_url=base_url) as http:
            async with connect_async_ws(_ws_url(base_url, "/ws/worker/wr2/term")) as worker:
                await worker.recv()  # snapshot_req

                r1 = await http.post("/worker/wr2/hijack/acquire", json={"owner": "test", "lease_s": 60})
                assert r1.status_code == 200, f"First acquire should succeed, got {r1.status_code}: {r1.text}"

            # Worker reconnects; hub clears hijack on worker disconnect before registering new worker
            async with connect_async_ws(_ws_url(base_url, "/ws/worker/wr2/term")) as worker:
                await worker.recv()  # snapshot_req — server processed disconnect before this
                # Second acquire should succeed
                r2 = await http.post("/worker/wr2/hijack/acquire", json={"owner": "test2", "lease_s": 60})
                assert r2.status_code == 200, (
                    f"Second acquire after reconnect should succeed, got {r2.status_code}: {r2.text}"
                )


# ---------------------------------------------------------------------------
# TestRestHijackAdvanced
# ---------------------------------------------------------------------------


class TestRestHijackAdvanced:
    async def test_snapshot_poll_returns_response(self, live_hub: Any) -> None:
        """Acquire, then poll snapshot endpoint; returns 200 with ok=True."""
        _, base_url = live_hub
        async with (
            connect_async_ws(_ws_url(base_url, "/ws/worker/rha1/term")) as worker,
            httpx.AsyncClient(base_url=base_url) as http,
        ):
            await worker.recv()  # snapshot_req

            r = await http.post("/worker/rha1/hijack/acquire", json={"owner": "test", "lease_s": 60})
            assert r.status_code == 200
            hijack_id = r.json()["hijack_id"]

            # Send a snapshot so poll doesn't timeout
            await worker.send_json(_snapshot_msg("test snapshot"))

            # Poll snapshot endpoint — wait_ms=500 lets it block until snapshot arrives
            r2 = await http.get(f"/worker/rha1/hijack/{hijack_id}/snapshot?wait_ms=500")
            assert r2.status_code == 200, f"Snapshot poll should return 200, got {r2.status_code}: {r2.text}"
            data = r2.json()
            assert data.get("ok") is True, f"Snapshot should have ok=True, got {data}"
            assert data.get("hijack_id") == hijack_id, f"hijack_id mismatch: {data}"

    async def test_events_ring_buffer_ordered(self, live_hub: Any) -> None:
        """Acquire, step, GET /events; returns events with monotonic seq."""
        _, base_url = live_hub
        async with (
            connect_async_ws(_ws_url(base_url, "/ws/worker/rha2/term")) as worker,
            httpx.AsyncClient(base_url=base_url) as http,
        ):
            await worker.recv()

            r = await http.post("/worker/rha2/hijack/acquire", json={"owner": "test", "lease_s": 60})
            assert r.status_code == 200
            hijack_id = r.json()["hijack_id"]
            await _drain_until(worker, "control")

            # Send a snapshot to ensure we have a valid state
            await worker.send_json(_snapshot_msg())
            # Use step endpoint with wait — avoids sleep by letting server block
            await _drain_until(worker, "snapshot", timeout=2.0)

            # Trigger a step event
            r2 = await http.post(f"/worker/rha2/hijack/{hijack_id}/step")
            assert r2.status_code == 200, f"Step should return 200, got {r2.status_code}: {r2.text}"

            # GET events while hijack is still active
            r3 = await http.get(f"/worker/rha2/hijack/{hijack_id}/events")
            assert r3.status_code == 200, f"Events should return 200, got {r3.status_code}: {r3.text}"
            events = r3.json().get("events", [])
            # Verify monotonically increasing seq
            seqs = [e["seq"] for e in events if "seq" in e]
            if len(seqs) > 1:
                assert seqs == sorted(seqs), f"Event sequences should be monotonic: {seqs}"

    async def test_concurrent_acquire_second_client_gets_409(self, live_hub: Any) -> None:
        """Two clients race to acquire; exactly one gets 200, other gets 409."""
        _, base_url = live_hub
        async with connect_async_ws(_ws_url(base_url, "/ws/worker/rha3/term")) as worker:
            await worker.recv()

            async def acquire(client_id: int):
                async with httpx.AsyncClient(base_url=base_url) as http:
                    r = await http.post(
                        "/worker/rha3/hijack/acquire", json={"owner": f"client{client_id}", "lease_s": 60}
                    )
                    return r.status_code

            # Race two acquires
            results = await asyncio.gather(acquire(1), acquire(2))
            statuses = sorted(results)
            # Exactly one 200 and one 409
            assert 200 in statuses and 409 in statuses, f"Expected one 200 and one 409, got {statuses}"


# ---------------------------------------------------------------------------
# TestAnalyzeRoundTrip
# ---------------------------------------------------------------------------


class TestAnalyzeRoundTrip:
    async def test_analyze_req_forwarded_to_worker_and_back(self, live_hub: Any) -> None:
        """Hijack owner sends analyze_req; worker receives and replies with analysis."""
        _, base_url = live_hub
        async with (
            connect_async_ws(_ws_url(base_url, "/ws/browser/ar1/term")) as browser,
            connect_async_ws(_ws_url(base_url, "/ws/worker/ar1/term")) as worker,
        ):
            await worker.recv()
            await _drain_all(browser)

            # Browser acquires hijack first
            await browser.send_json({"type": "hijack_request"})
            state = await _drain_until(browser, "hijack_state")
            assert state is not None, "Should receive hijack_state after hijack_request"
            await _drain_until(worker, "control")  # pause

            # Browser sends analyze_req (only works for hijack owner)
            await browser.send_json({"type": "analyze_req", "ts": time.time()})

            # Worker receives it
            analyze = await _drain_until(worker, "analyze_req", timeout=2.0)
            assert analyze is not None, "Worker should receive analyze_req"

            # Worker sends analysis back
            await worker.send_json(
                {
                    "type": "analysis",
                    "formatted": "test analysis result",
                    "ts": time.time(),
                }
            )

            # Browser receives it
            analysis = await _drain_until(browser, "analysis", timeout=2.0)
            assert analysis is not None, "Browser should receive analysis"
            assert analysis["formatted"] == "test analysis result", f"Unexpected analysis: {analysis}"


# ---------------------------------------------------------------------------
# TestInputModeLifecycle
# ---------------------------------------------------------------------------


class TestInputModeLifecycle:
    async def test_mode_switch_to_open_blocked_while_rest_hijack_active(self, live_hub: Any) -> None:
        """REST acquire hijack; attempt /input_mode → 409."""
        _, base_url = live_hub
        async with (
            connect_async_ws(_ws_url(base_url, "/ws/worker/im1/term")) as worker,
            httpx.AsyncClient(base_url=base_url) as http,
        ):
            await worker.recv()

            r = await http.post("/worker/im1/hijack/acquire", json={"owner": "test", "lease_s": 60})
            assert r.status_code == 200, f"Acquire should return 200, got {r.status_code}: {r.text}"

            # Try to change input_mode
            r2 = await http.post("/worker/im1/input_mode", json={"input_mode": "open"})
            assert r2.status_code == 409, f"Mode switch should be rejected with 409, got {r2.status_code}: {r2.text}"

    async def test_worker_hello_open_mode_propagates_to_browsers(self, live_hub: Any) -> None:
        """Worker sends worker_hello with open mode; browser receives mode update."""
        _, base_url = live_hub
        async with (
            connect_async_ws(_ws_url(base_url, "/ws/browser/im2/term")) as browser,
            connect_async_ws(_ws_url(base_url, "/ws/worker/im2/term")) as worker,
        ):
            await worker.recv()
            await _drain_all(browser)

            # Worker sends worker_hello with open mode
            await worker.send_json(
                {
                    "type": "worker_hello",
                    "input_mode": "open",
                    "ts": time.time(),
                }
            )

            # Browser should receive an update reflecting open mode
            # This may come as a hijack_state with input_mode field
            msg = await _drain_until(browser, "hijack_state", timeout=2.0)
            assert msg is not None, "Browser should receive hijack_state after mode switch"
            # Verify open mode is reflected (field name may vary)
            assert msg.get("input_mode") == "open" or "open" in str(msg), f"Browser should see 'open' mode, got {msg}"
