#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared fixtures for multi-session e2e scenarios."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
import uvicorn

from provide.terminal.hijack.hub import EventBus
from provide.terminal.server.app import create_server_app
from provide.terminal.server.config import config_from_mapping

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}


@pytest.fixture()
async def two_session_server() -> Any:
    """Two-session (s1, s2) live server with EventBus. Yields (hub, base_url)."""
    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 0},
            "auth": {"mode": "dev"},
            "sessions": [
                {
                    "session_id": "s1",
                    "display_name": "Session 1",
                    "connector_type": "shell",
                    "auto_start": False,
                },
                {
                    "session_id": "s2",
                    "display_name": "Session 2",
                    "connector_type": "shell",
                    "auto_start": False,
                },
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
            raise RuntimeError("two_session_server: uvicorn startup timeout")
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


def ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


def snapshot_msg(screen: str = "$ test", session_label: str = "") -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": screen + (f" [{session_label}]" if session_label else ""),
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": f"hash-{session_label}",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": f"ms-{session_label}"},
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


async def watch_events(
    base_url: str,
    session_id: str,
    *,
    timeout_ms: int = 5000,
    max_events: int = 1,
    event_types: str | None = None,
) -> Any:
    params: dict[str, Any] = {"timeout_ms": timeout_ms, "max_events": max_events}
    if event_types:
        params["event_types"] = event_types
    async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=15.0) as http:
        return await http.get(f"/api/sessions/{session_id}/events/watch", params=params)
