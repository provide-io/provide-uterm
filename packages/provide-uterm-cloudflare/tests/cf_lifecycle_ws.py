# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public-route WebSocket plumbing shared by the Cloudflare lifecycle adapter.

Extracted verbatim from ``test_session_lifecycle_security_scenarios.py`` so
that module stays inside the repository's 777-line per-file cap once the
ownership-handoff scenario is added.  Nothing here stubs, mocks, or
short-circuits the edge: every helper speaks the real DLE/STX control channel
to a real ``workerd`` instance over a real WebSocket.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import websockets

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder, DataChunk, encode_control_frame

WORKER_TOKEN = "lifecycle-worker-token-padded-beyond-32-characters"


def observation(scenario: dict[str, Any], defaults: dict[str, Any], **values: Any) -> dict[str, Any]:
    return {"id": scenario["id"], "status": scenario["backends"]["cloudflare"]["status"], **defaults, **values}


def ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://", 1) + path


def decode(raw: str | bytes) -> list[dict[str, Any]]:
    if not isinstance(raw, str):
        return []
    events: list[dict[str, Any]] = []
    for event in ControlFrameDecoder().feed(raw):
        if isinstance(event, ControlChunk):
            events.append(event.control)
        elif isinstance(event, DataChunk):
            events.append({"type": "data", "data": event.data})
    return events


async def receive_matching(
    websocket: Any,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 4,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(websocket.recv(), timeout=max(0.01, deadline - asyncio.get_running_loop().time()))
        for event in decode(raw):
            if predicate(event):
                return event
    raise TimeoutError("matching WebSocket frame was not observed")


async def matching_count(
    websocket: Any,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 0.2,
) -> int:
    count = 0
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=deadline - asyncio.get_running_loop().time())
        except TimeoutError:
            break
        count += sum(predicate(event) for event in decode(raw))
    return count


async def collect_matching(
    websocket: Any,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 0.3,
) -> list[dict[str, Any]]:
    """Drain a bounded window and return every frame the predicate accepts.

    Used where "and nothing else arrived" is part of the claim: a leaked frame
    has to show up in the returned list rather than be silently skipped over.
    """
    collected: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=deadline - asyncio.get_running_loop().time())
        except TimeoutError:
            break
        collected.extend(event for event in decode(raw) if predicate(event))
    return collected


async def send_control(websocket: Any, frame: dict[str, Any]) -> None:
    await websocket.send(encode_control_frame(frame))


async def drain_worker_startup(worker: Any) -> None:
    # Registration is completed eagerly before the 101 response.  The CF
    # protocol doesn't emit an unsolicited worker hello/snapshot request.
    assert worker.state.name == "OPEN"


async def drain_browser_startup(browser: Any) -> dict[str, Any]:
    return await receive_matching(browser, lambda event: event.get("type") == "hello")


async def acquire(browser: Any, worker: Any) -> None:
    await send_control(browser, {"type": "hijack_request"})
    await receive_matching(worker, lambda event: event.get("type") == "control" and event.get("action") == "pause")
    await receive_matching(browser, lambda event: event.get("type") == "hijack_state" and event.get("owner") == "me")


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def connect(url: str, *, additional_headers: dict[str, str]) -> Any:
    """Open a socket with bounded cleanup for workerd hibernation sockets."""
    return websockets.connect(
        url,
        additional_headers=additional_headers,
        close_timeout=0.2,
        ping_interval=None,
    )
