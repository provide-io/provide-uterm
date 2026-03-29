#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared fixtures for multi-browser e2e scenarios."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn

from provide.terminal.client import connect_async_ws
from provide.terminal.hijack.hub import EventBus
from provide.terminal.server.app import create_server_app
from provide.terminal.server.config import config_from_mapping

# ---------------------------------------------------------------------------
# Header constants
# ---------------------------------------------------------------------------

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}
OPERATOR_H = {"X-Uterm-Principal": "op-user", "X-Uterm-Role": "operator"}
VIEWER_H = {"X-Uterm-Principal": "view-user", "X-Uterm-Role": "viewer"}


# ---------------------------------------------------------------------------
# Fixture: live server with EventBus, single session
# ---------------------------------------------------------------------------


@pytest.fixture()
async def live_server() -> Any:
    """Single-session live server with EventBus injected. Yields (hub, base_url)."""
    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 0},
            "auth": {"mode": "dev"},
            "sessions": [
                {
                    "session_id": "s1",
                    "display_name": "Test Session",
                    "connector_type": "shell",
                    "auto_start": False,
                }
            ],
        }
    )
    app = create_server_app(cfg)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while not server.started:
        if loop.time() > deadline:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=2.0)
            raise RuntimeError("live_server: uvicorn startup timeout")
        await asyncio.sleep(0.05)

    port: int = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    hub = app.state.uterm_registry._hub
    hub._event_bus = EventBus()

    try:
        yield hub, base_url
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


def snapshot_msg(screen: str = "$ test") -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": "mbtest",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": "mb"},
        "ts": time.time(),
    }


async def drain_until(ws: Any, type_: str, timeout: float = 3.0) -> dict[str, Any] | None:
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


async def drain_all(ws: Any, timeout: float = 0.4) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.1)
            msgs.append(json.loads(raw))
        except TimeoutError:
            continue
    return msgs


@asynccontextmanager
async def connect_browser(base_url: str, session_id: str, role: str = "admin") -> Any:
    """Connect a browser WS with the given role header."""
    headers = {"X-Uterm-Principal": f"{role}-user", "X-Uterm-Role": role}
    url = ws_url(base_url, f"/ws/browser/{session_id}/term")
    async with connect_async_ws(url, additional_headers=headers) as ws:
        yield ws


async def long_poll(
    base_url: str,
    session_id: str,
    *,
    timeout_ms: int = 5000,
    max_events: int = 1,
    event_types: str | None = None,
    pattern: str | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """Issue a single GET /api/sessions/{id}/events/watch and return the response."""
    params: dict[str, Any] = {"timeout_ms": timeout_ms, "max_events": max_events}
    if event_types:
        params["event_types"] = event_types
    if pattern:
        params["pattern"] = pattern
    h = {**(headers or {}), **ADMIN_H}
    async with httpx.AsyncClient(base_url=base_url, headers=h, timeout=30.0) as http:
        return await http.get(f"/api/sessions/{session_id}/events/watch", params=params)
