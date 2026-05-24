#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
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

from provide.uterm.bridge.hub import EventBus
from provide.uterm.server.app import create_server_app
from provide.uterm.server.config import config_from_mapping


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
            "auth": {
                "mode": "header",
                "header_mode_acknowledged": True,
                "worker_bearer_token": "test-bearer-token-32-chars-long-x",
            },
            "webhooks": {"allow_loopback_destinations": True},
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


async def wait_for_subscribers(
    hub: Any,
    worker_id: str,
    expected: int,
    *,
    timeout: float = 5.0,
    interval: float = 0.01,
) -> None:
    """Poll the EventBus until *worker_id* has at least *expected* subscribers.

    Replaces the brittle ``await asyncio.sleep(0.1)`` hope-and-pray pattern in
    fan-out tests. HTTP long-poll subscribers register themselves with
    :class:`EventBus` from inside their request handler; on a loaded CI runner
    the registration can take >100ms, after which any events fired in the
    interim are missed and the subscriber returns 0 events.
    """
    bus = hub._event_bus
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        # ``EventBus._subs`` is the live registry — reading length is safe.
        if len(bus._subs.get(worker_id, [])) >= expected:
            return
        await asyncio.sleep(interval)
    current = len(bus._subs.get(worker_id, []))
    raise TimeoutError(
        f"wait_for_subscribers({worker_id!r}, expected={expected}) timed out after {timeout}s; saw {current}"
    )
