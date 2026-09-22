#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared fixtures for the Python session-lifecycle security scenario adapter.

``test_session_lifecycle_security_scenarios`` and its sibling
``session_lifecycle_scenario_ops`` both drive the really-served app through
these helpers. Keeping them in their own module lets both import them at
module level without importing each other.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder, DataChunk, encode_control_frame
from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.config_schema import SessionDefinition

WORKER_TOKEN = "test-bearer-token-32-chars-long-x"
POLICY_SECRET = "lifecycle-policy-secret"  # pragma: allowlist secret
ADMIN_HEADERS = {"X-Uterm-Principal": "lifecycle-admin", "X-Uterm-Role": "admin"}
WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def _ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://", 1) + path


def _principal_headers(subject: str) -> dict[str, str]:
    return {"X-Uterm-Principal": subject, "X-Uterm-Role": "admin"}


def _observation(scenario: dict[str, Any], defaults: dict[str, Any], **values: Any) -> dict[str, Any]:
    return {"id": scenario["id"], "status": "served", **defaults, **values}


def _configured_app(
    worker_id: str,
    *,
    max_connections: int = 25,
    policy_url: str | None = None,
    fail_browser_setup_once: bool = False,
) -> FastAPI:
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = WORKER_TOKEN
    config.server.host = "127.0.0.1"
    config.server.port = 0
    config.max_connections_per_principal = max_connections
    config.sessions = [
        SessionDefinition(
            session_id=worker_id,
            display_name=worker_id,
            connector_type="shell",
            input_mode="hijack",
            auto_start=False,
            visibility="public",
        )
    ]
    if policy_url is not None:
        config.governance.policy_webhook_url = policy_url
        config.governance.policy_webhook_secret = POLICY_SECRET
        config.governance.policy_webhook_timeout_s = 1.0
        config.webhooks.allow_loopback_destinations = True
    app = create_server_app(config, api_only=True)
    if fail_browser_setup_once:
        hub = app.state.uterm_hub
        activate_browser_broadcasts = hub.activate_browser_broadcasts
        app.state.lifecycle_setup_failure_triggered = False

        async def fail_first_browser_setup(worker_id: str, websocket: Any) -> None:
            if not app.state.lifecycle_setup_failure_triggered:
                app.state.lifecycle_setup_failure_triggered = True
                raise RuntimeError("injected lifecycle browser setup failure")
            await activate_browser_broadcasts(worker_id, websocket)

        hub.activate_browser_broadcasts = fail_first_browser_setup  # type: ignore[method-assign]
    return app


@asynccontextmanager
async def _serve(app: FastAPI, label: str) -> AsyncIterator[str]:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="critical",
        ws_max_size=2_500_000,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while not server.started:
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"{label}: uvicorn startup timeout")
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=5.0)


def _decode_events(raw: str | bytes) -> list[dict[str, Any]]:
    if not isinstance(raw, str):
        return []
    decoded: list[dict[str, Any]] = []
    for event in ControlFrameDecoder().feed(raw):
        if isinstance(event, ControlChunk):
            decoded.append(event.control)
        elif isinstance(event, DataChunk):
            decoded.append({"type": "data", "data": event.data})
    return decoded


async def _receive_matching(
    websocket: Any,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        remaining = max(0.01, deadline - asyncio.get_running_loop().time())
        raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
        for event in _decode_events(raw):
            if predicate(event):
                return event
    raise TimeoutError("matching WebSocket frame was not observed")


async def _receive_through(
    websocket: Any,
    barrier: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 2.0,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        remaining = max(0.01, deadline - asyncio.get_running_loop().time())
        raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
        decoded = _decode_events(raw)
        events.extend(decoded)
        if any(barrier(event) for event in decoded):
            return events
    raise TimeoutError("WebSocket ordering barrier was not observed")


async def _matching_count(
    websocket: Any,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 0.15,
) -> int:
    count = 0
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=deadline - asyncio.get_running_loop().time())
        except TimeoutError:
            break
        for event in _decode_events(raw):
            count += int(predicate(event))
    return count


async def _send_control(websocket: Any, frame: dict[str, Any]) -> None:
    await websocket.send(encode_control_frame(frame))


async def _drain_browser_startup(websocket: Any) -> dict[str, Any]:
    hello = await _receive_matching(websocket, lambda event: event.get("type") == "hello")
    await _receive_matching(websocket, lambda event: event.get("type") == "hijack_state")
    return hello


async def _drain_worker_startup(websocket: Any) -> None:
    await _receive_matching(websocket, lambda event: event.get("type") == "snapshot_req")


async def _acquire_browser_owner(browser: Any, worker: Any) -> None:
    await _send_control(browser, {"type": "hijack_request"})
    await _receive_matching(
        worker,
        lambda event: event.get("type") == "control" and event.get("action") == "pause",
    )
    await _receive_matching(
        browser,
        lambda event: event.get("type") == "hijack_state" and event.get("owner") == "me",
    )


@asynccontextmanager
async def _policy_server(
    decision: str,
    *,
    timeout_s: int | None = None,
) -> AsyncIterator[tuple[str, list[tuple[bytes, dict[str, str]]]]]:
    calls: list[tuple[bytes, dict[str, str]]] = []
    app = FastAPI()

    @app.post("/policy")
    async def policy(request: Request) -> Response:
        calls.append((await request.body(), dict(request.headers)))
        if decision == "unavailable":
            return Response(status_code=503)
        body: dict[str, Any] = {"action": decision, "reason": f"fixture_{decision}"}
        if timeout_s is not None:
            body["timeout_s"] = timeout_s
        return JSONResponse(body)

    async with _serve(app, f"python governance {decision}") as base_url:
        yield f"{base_url}/policy", calls
