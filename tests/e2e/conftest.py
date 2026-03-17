#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""E2E test helpers: WS utilities, message builders, draining."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any


def _ws_url(base_url: str, path: str) -> str:
    """Convert http to ws URL."""
    return base_url.replace("http://", "ws://") + path


def _snapshot_msg(screen: str = "test screen", prompt_id: str = "test_prompt") -> dict[str, Any]:
    """Build a valid snapshot message."""
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": "test-hash",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": prompt_id},
        "ts": time.time(),
    }


async def _drain_until(ws: Any, type_: str, timeout: float = 3.0) -> dict[str, Any] | None:
    """Drain messages until one matches the type."""
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


async def _drain_all(ws: Any, timeout: float = 0.5) -> list[dict[str, Any]]:
    """Drain all available messages."""
    msgs: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.1)
            msgs.append(json.loads(raw))
        except TimeoutError:
            continue
    return msgs


async def _poll_until_status(
    http: Any,
    url: str,
    expected_status: int,
    *,
    timeout: float = 2.0,
) -> Any:
    """Poll an HTTP endpoint until it returns the expected status code.

    Returns the final response. Useful for avoiding sleeps when waiting for
    server-side state to propagate before making an assertion.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    last_response = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            last_response = await http.get(url)
            if last_response.status_code == expected_status:
                return last_response
        except Exception:
            pass
        await asyncio.sleep(0.05)
    return last_response


async def _wait_for_server(server: Any, task: Any, label: str, *, timeout: float = 5.0) -> None:
    """Poll until uvicorn server.started is True or raise RuntimeError on timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not server.started:
        if loop.time() > deadline:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=2.0)
            raise RuntimeError(f"{label}: uvicorn startup timeout")
        await asyncio.sleep(0.05)
