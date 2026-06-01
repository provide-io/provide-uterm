#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared fixtures for real-world scenario e2e tests."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from provide.uterm.client import connect_async_ws

from provide.uterm.server.bridge.hub import InMemoryResumeStore, TermHub
from tests.e2e._live_server import live_server_with_bus

# ---------------------------------------------------------------------------
# Header constants for role-based auth (dev mode)
# ---------------------------------------------------------------------------

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}
OPERATOR_H = {"X-Uterm-Principal": "op-user", "X-Uterm-Role": "operator"}
VIEWER_H = {"X-Uterm-Principal": "view-user", "X-Uterm-Role": "viewer"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


def snapshot_msg(
    screen: str = "$ test",
    cols: int = 80,
    rows: int = 25,
    prompt_id: str = "scenario",
) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 0, "y": 0},
        "cols": cols,
        "rows": rows,
        "screen_hash": f"hash-{screen[:8]}",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": prompt_id},
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


async def drain_all(ws: Any, timeout: float = 0.5) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        remaining = deadline - asyncio.get_running_loop().time()
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(0.1, remaining))
            msgs.append(json.loads(raw))
        except TimeoutError:
            break
    return msgs


@asynccontextmanager
async def connect_browser(base_url: str, session_id: str, role: str = "admin") -> Any:
    """Connect a browser WS with the given role header."""
    headers = {"X-Uterm-Principal": f"{role}-user", "X-Uterm-Role": role}
    url = ws_url(base_url, f"/ws/browser/{session_id}/term")
    async with connect_async_ws(url, additional_headers=headers) as ws:
        yield ws


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def single_session_server() -> Any:
    """Single-session live server (role-aware). Yields (hub, base_url)."""
    sessions = [
        {"session_id": "s1", "display_name": "Scenario Session", "connector_type": "shell", "auto_start": False},
    ]
    async with live_server_with_bus(sessions, label="scenario_single") as result:
        yield result


@pytest.fixture()
async def three_session_server() -> Any:
    """Three-session live server. Yields (hub, base_url)."""
    sessions = [
        {"session_id": "s1", "display_name": "S1", "connector_type": "shell", "auto_start": False},
        {"session_id": "s2", "display_name": "S2", "connector_type": "shell", "auto_start": False},
        {"session_id": "s3", "display_name": "S3", "connector_type": "shell", "auto_start": False},
    ]
    async with live_server_with_bus(sessions, label="scenario_three") as result:
        yield result


@pytest.fixture()
async def resume_hub() -> Any:
    """TermHub with InMemoryResumeStore on random port. Yields (hub, base_url)."""
    hub = TermHub(
        resolve_browser_role=lambda _ws, _worker_id: "admin",
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
                raise RuntimeError("resume_hub: uvicorn startup timeout")
            await asyncio.sleep(0.05)

        port: int = server.servers[0].sockets[0].getsockname()[1]
        yield hub, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)
