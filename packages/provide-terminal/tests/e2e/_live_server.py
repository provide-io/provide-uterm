#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared async context manager for e2e tests that need a live uvicorn server.

Usage::

    from tests.e2e._live_server import live_server_with_bus

    @pytest.fixture()
    async def live_server() -> Any:
        sessions = [{"session_id": "s1", "display_name": "Test", "connector_type": "shell", "auto_start": False}]
        async with live_server_with_bus(sessions) as (hub, base_url):
            yield hub, base_url
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import uvicorn

from provide.terminal.hijack.hub import EventBus
from provide.terminal.server.app import create_server_app
from provide.terminal.server.config import config_from_mapping


@contextlib.asynccontextmanager
async def live_server_with_bus(
    sessions: list[dict[str, Any]],
    *,
    label: str = "live_server",
    startup_timeout: float = 5.0,
    shutdown_timeout: float = 5.0,
) -> Any:
    """Spin up a live uvicorn server with EventBus injected.

    Yields ``(hub, base_url)`` where *hub* is the TermHub instance with a fresh
    :class:`EventBus` attached and *base_url* is ``http://127.0.0.1:<port>``.
    """
    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 0},
            "auth": {"mode": "dev"},
            "sessions": sessions,
        }
    )
    app = create_server_app(cfg)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    loop = asyncio.get_running_loop()
    deadline = loop.time() + startup_timeout
    while not server.started:
        if loop.time() > deadline:
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=2.0)
            raise RuntimeError(f"{label}: uvicorn startup timeout")
        await asyncio.sleep(0.05)

    port: int = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    hub = app.state.uterm_registry._hub
    hub._event_bus = EventBus()

    try:
        yield hub, base_url
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=shutdown_timeout)
