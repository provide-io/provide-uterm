#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E: Webhook delivery via live uvicorn server.

Scenarios
---------
1. Shell session worker sends snapshot → webhook is delivered.
2. event_types filter — snapshot filtered, hijack_acquired delivered.
3. Webhook unregistered → no more delivery after unregister.
4. HMAC-SHA256 signature header present when secret configured.
5. Worker disconnect sentinel → delivery loop stops, no spurious delivery.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request

from provide.uterm.client import connect_async_ws
from tests.e2e._live_server import live_server_with_bus

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}


# ---------------------------------------------------------------------------
# In-process webhook receiver helper
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def webhook_receiver() -> Any:
    """Start a minimal FastAPI server to capture POSTed webhook payloads.

    Yields ``(hook_url, received_queue)`` where *hook_url* is the URL to
    configure as the webhook destination and *received_queue* is an asyncio
    queue that each incoming payload is pushed to.
    """
    received: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    receiver_app = FastAPI()

    @receiver_app.post("/hook")
    async def _receive(request: Request) -> dict[str, Any]:
        body = await request.body()
        await received.put(json.loads(body))
        return {"ok": True}

    config = uvicorn.Config(receiver_app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while not server.started:
        if loop.time() > deadline:
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=2.0)
            raise RuntimeError("webhook_receiver: startup timeout")
        await asyncio.sleep(0.05)

    port: int = server.servers[0].sockets[0].getsockname()[1]
    hook_url = f"http://127.0.0.1:{port}/hook"

    try:
        yield hook_url, received
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=3.0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def shell_server() -> Any:
    sessions = [{"session_id": "wh1", "display_name": "Webhook Shell", "connector_type": "shell", "auto_start": False}]
    async with live_server_with_bus(sessions, label="wh_shell") as result:
        yield result


@pytest.fixture()
async def multi_session_server() -> Any:
    sessions = [
        {"session_id": "wh-shell", "display_name": "WH Shell", "connector_type": "shell", "auto_start": False},
        {"session_id": "wh-telnet", "display_name": "WH Telnet", "connector_type": "shell", "auto_start": False},
    ]
    async with live_server_with_bus(sessions, label="wh_multi") as result:
        yield result


def ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


def snapshot_msg(screen: str = "$ test") -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": "wh-hash",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": "wh-p"},
        "ts": time.time(),
    }


# ---------------------------------------------------------------------------
# 1. Shell session snapshot is delivered to webhook
# ---------------------------------------------------------------------------


async def test_webhook_snapshot_delivered(shell_server: Any) -> None:
    hub, base_url = shell_server

    async with webhook_receiver() as (hook_url, received):
        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H) as http:
            resp = await http.post("/api/sessions/wh1/webhooks", json={"url": hook_url})
            assert resp.status_code == 200

        async with connect_async_ws(ws_url(base_url, "/ws/worker/wh1/term")) as worker:
            await worker.recv()  # snapshot_req
            await asyncio.sleep(0.1)
            await worker.send(json.dumps(snapshot_msg("$ webhook e2e")))

            payload = await asyncio.wait_for(received.get(), timeout=8.0)

    assert payload["session_id"] == "wh1"
    assert payload["event"]["type"] == "snapshot"
    assert payload["event"]["data"]["screen"] == "$ webhook e2e"
    assert "webhook_id" in payload
    assert "timestamp" in payload


# ---------------------------------------------------------------------------
# 2. event_types filter — snapshot filtered, hijack_acquired delivered
# ---------------------------------------------------------------------------


async def test_webhook_event_types_filter(shell_server: Any) -> None:
    hub, base_url = shell_server

    async with webhook_receiver() as (hook_url, received):
        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H) as http:
            resp = await http.post(
                "/api/sessions/wh1/webhooks",
                json={"url": hook_url, "event_types": ["hijack_acquired"]},
            )
            assert resp.status_code == 200

        async with connect_async_ws(ws_url(base_url, "/ws/worker/wh1/term")) as worker:
            await worker.recv()  # snapshot_req
            await asyncio.sleep(0.1)

            # snapshot — should be filtered. hijack_acquired (injected
            # immediately after) is the positive control: if the filter
            # leaks, the snapshot would dequeue first and the type
            # assertion below would fail.
            await worker.send(json.dumps(snapshot_msg("$ filtered")))
            event_bus = hub.event_bus
            assert event_bus is not None
            event_bus._enqueue(  # type: ignore[attr-defined]
                "wh1",
                {"type": "hijack_acquired", "seq": 1, "ts": time.time(), "data": {}},
            )

            payload = await asyncio.wait_for(received.get(), timeout=8.0)
            # No second delivery should ever arrive — confirm by draining.
            with contextlib.suppress(TimeoutError):
                extra = await asyncio.wait_for(received.get(), timeout=0.5)
                raise AssertionError(f"event_types filter let snapshot through: {extra}")

    assert payload["event"]["type"] == "hijack_acquired"


# ---------------------------------------------------------------------------
# 3. Webhook unregistered → no delivery after unregister
# ---------------------------------------------------------------------------


async def test_webhook_unregister_stops_delivery(shell_server: Any) -> None:
    hub, base_url = shell_server

    async with webhook_receiver() as (hook_url, received):
        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H) as http:
            reg_resp = await http.post("/api/sessions/wh1/webhooks", json={"url": hook_url})
            assert reg_resp.status_code == 200
            webhook_id = reg_resp.json()["webhook_id"]

            # Unregister before any events
            del_resp = await http.delete(f"/api/sessions/wh1/webhooks/{webhook_id}")
            assert del_resp.status_code == 200
            assert del_resp.json()["ok"] is True

        async with connect_async_ws(ws_url(base_url, "/ws/worker/wh1/term")) as worker:
            await worker.recv()
            await asyncio.sleep(0.1)
            await worker.send(json.dumps(snapshot_msg("$ after unregister")))
            await asyncio.sleep(0.5)

    # Nothing delivered — queue should be empty
    assert received.empty()


# ---------------------------------------------------------------------------
# 4. HMAC signature is present when secret configured
# ---------------------------------------------------------------------------


async def test_webhook_hmac_signature_header(shell_server: Any) -> None:
    import hashlib
    import hmac as stdlib_hmac

    hub, base_url = shell_server
    secret = "uterm-test-secret-32-byte-minimum-key"

    # We'll capture the raw request to check headers.
    captured: list[tuple[bytes, dict[str, str]]] = []
    receiver_app = FastAPI()

    @receiver_app.post("/signed")
    async def _receive(request: Request) -> dict[str, Any]:
        body = await request.body()
        captured.append((body, dict(request.headers)))
        return {"ok": True}

    config = uvicorn.Config(receiver_app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while not server.started:
        if loop.time() > deadline:
            break
        await asyncio.sleep(0.05)
    port: int = server.servers[0].sockets[0].getsockname()[1]
    hook_url = f"http://127.0.0.1:{port}/signed"

    try:
        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H) as http:
            resp = await http.post(
                "/api/sessions/wh1/webhooks",
                json={"url": hook_url, "secret": secret},
            )
            assert resp.status_code == 200

        async with connect_async_ws(ws_url(base_url, "/ws/worker/wh1/term")) as worker:
            await worker.recv()
            await asyncio.sleep(0.1)
            await worker.send(json.dumps(snapshot_msg("$ signed")))
            # Wait for delivery
            deadline2 = loop.time() + 8.0
            while not captured and loop.time() < deadline2:
                await asyncio.sleep(0.05)
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=3.0)

    assert captured, "no webhook delivery received"
    body, headers = captured[0]
    sig_header = headers.get("x-uterm-signature", "")
    assert sig_header.startswith("sha256=")
    expected = "sha256=" + stdlib_hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sig_header == expected


# ---------------------------------------------------------------------------
# 5. Worker disconnect — delivery loop stops, no spurious deliveries
# ---------------------------------------------------------------------------


async def test_webhook_loop_stops_on_worker_disconnect(shell_server: Any) -> None:
    hub, base_url = shell_server

    async with webhook_receiver() as (hook_url, received):
        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H) as http:
            resp = await http.post("/api/sessions/wh1/webhooks", json={"url": hook_url})
            assert resp.status_code == 200

        async with connect_async_ws(ws_url(base_url, "/ws/worker/wh1/term")) as worker:
            await worker.recv()
            await asyncio.sleep(0.1)
        # Worker context exits → disconnect sentinel delivered

        # Give delivery loop time to process sentinel
        await asyncio.sleep(0.3)

        # Now send an event after worker disconnected — nothing delivered
        event_bus = hub.event_bus
        if event_bus is not None:
            event_bus._enqueue(  # type: ignore[attr-defined]
                "wh1",
                {"type": "snapshot", "seq": 99, "ts": time.time(), "data": {"screen": "$ after"}},
            )
        await asyncio.sleep(0.3)

    assert received.empty()


# ---------------------------------------------------------------------------
# 6. List webhooks reflects registered state
# ---------------------------------------------------------------------------


async def test_webhook_list_via_api(shell_server: Any) -> None:
    hub, base_url = shell_server

    async with webhook_receiver() as (hook_url, _), httpx.AsyncClient(base_url=base_url, headers=ADMIN_H) as http:
        # Register two webhooks
        await http.post("/api/sessions/wh1/webhooks", json={"url": hook_url})
        await http.post("/api/sessions/wh1/webhooks", json={"url": hook_url + "2"})

        list_resp = await http.get("/api/sessions/wh1/webhooks")
        assert list_resp.status_code == 200
        data = list_resp.json()

    assert len(data["webhooks"]) == 2
