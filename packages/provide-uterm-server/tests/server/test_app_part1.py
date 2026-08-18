#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Live integration tests for the hosted terminal server app."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

import httpx2
import pytest
import uvicorn

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder, DataChunk
from provide.uterm.server import create_server_app, default_server_config

if TYPE_CHECKING:
    from collections.abc import Generator


def _ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


_WS_DECODERS: WeakKeyDictionary[Any, ControlFrameDecoder] = WeakKeyDictionary()
_WS_PENDING: WeakKeyDictionary[Any, list[dict[str, Any]]] = WeakKeyDictionary()


async def _drain_until(ws: Any, type_: str, timeout: float = 3.0) -> dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            pending = _WS_PENDING.setdefault(ws, [])
            if pending:
                msg = pending.pop(0)
                if msg.get("type") == type_:
                    return msg
                continue
            raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
            decoder = _WS_DECODERS.setdefault(ws, ControlFrameDecoder())
            events = decoder.feed(raw)
            for event in events:
                if isinstance(event, ControlChunk):
                    pending.append(event.control)
                elif isinstance(event, DataChunk):
                    pending.append({"type": "term", "data": event.data})
            if not pending:
                continue
            msg = pending.pop(0)
            if msg.get("type") == type_:
                return msg
        except TimeoutError:
            continue
    return None


async def _drain_snapshot_containing(ws: Any, text: str, timeout: float = 10.0) -> dict[str, Any] | None:
    """Drain snapshots until one whose ``screen`` contains *text* is seen.

    Tests against a real worker need this rather than ``_drain_until("snapshot")``
    because the first snapshot is often just the session banner — the actual
    content from the connector arrives in a later snapshot once the upstream
    process (SSH echo, telnet greeting, etc.) has produced output.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        snap = await _drain_until(ws, "snapshot", timeout=min(0.5, remaining))
        if snap is None:
            continue
        if text in str(snap.get("screen", "")):
            return snap
    return None


async def _delete_session(base_url: str, session_id: str) -> None:
    async def _delete() -> None:
        async with httpx2.AsyncClient(base_url=base_url, timeout=5.0) as http:
            await http.delete(f"/api/sessions/{session_id}")

    with contextlib.suppress(Exception):
        await asyncio.wait_for(_delete(), timeout=6.0)


@pytest.fixture()
def live_reference_server() -> Generator[str, None, None]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    base_url = f"http://127.0.0.1:{port}"
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.server.public_base_url = base_url
    app = create_server_app(config)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("reference server did not start")
        time.sleep(0.05)

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)
