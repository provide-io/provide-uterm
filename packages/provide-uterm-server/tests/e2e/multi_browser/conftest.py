#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
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

from provide.uterm.client import connect_async_ws
from tests.e2e._live_server import live_server_with_bus

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
    sessions = [{"session_id": "s1", "display_name": "Test Session", "connector_type": "shell", "auto_start": False}]
    async with live_server_with_bus(sessions, label="live_server (multi_browser)") as result:
        yield result


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


async def drain_until_hijack_state(
    ws: Any,
    *,
    hijacked: bool | None = None,
    owner: str | None = None,
    timeout: float = 3.0,
) -> dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = await drain_until(
            ws, "hijack_state", timeout=min(0.3, max(0.0, deadline - asyncio.get_running_loop().time()))
        )
        if state is None:
            continue
        if hijacked is not None and state.get("hijacked") is not hijacked:
            continue
        if owner is not None and state.get("owner") != owner:
            continue
        return state
    return None


async def drain_all(ws: Any, timeout: float = 0.4) -> list[dict[str, Any]]:
    """Collect all messages that arrive within *timeout* seconds.

    Stops collecting as soon as a recv() times out — no more messages are
    immediately available.  The *timeout* parameter acts as an upper bound.
    """
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


async def wait_for_event_subscriber(hub: Any, worker_id: str, *, min_count: int = 1, timeout: float = 3.0) -> None:
    event_bus = hub.event_bus
    assert event_bus is not None
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if event_bus.subscriber_count(worker_id) >= min_count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"EventBus subscriber for {worker_id!r} was not registered")


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
