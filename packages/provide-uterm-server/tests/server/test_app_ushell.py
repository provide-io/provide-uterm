#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E tests: UshellConnector through a live provide.uterm server."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any
from weakref import WeakKeyDictionary

import httpx
import pytest
import websockets

from provide.uterm.control_channel import ControlChannelDecoder, ControlChunk, DataChunk, encode_data
from provide.uterm.server import create_server_app, default_server_config


def _ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


_WS_DECODERS: WeakKeyDictionary[Any, ControlChannelDecoder] = WeakKeyDictionary()
_WS_PENDING: WeakKeyDictionary[Any, list[dict[str, Any]]] = WeakKeyDictionary()


async def _drain_until(ws: Any, type_: str, timeout: float = 5.0) -> dict[str, Any] | None:
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
            decoder = _WS_DECODERS.setdefault(ws, ControlChannelDecoder())
            for event in decoder.feed(raw):
                if isinstance(event, ControlChunk):
                    pending.append(event.control)
                elif isinstance(event, DataChunk):
                    pending.append({"type": "term", "data": event.data})
        except TimeoutError:
            continue
    return None


async def _drain_term_until(ws: Any, needle: str, timeout: float = 5.0) -> str:
    """Accumulate term chunks until *needle* appears in the combined output."""
    collected: list[str] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        chunk = await _drain_until(ws, "term", timeout=0.3)
        if chunk is None:
            continue
        collected.append(chunk["data"])
        if needle in "".join(collected):
            return "".join(collected)
    return "".join(collected)


@pytest.fixture()
def ushell_server() -> Any:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    base_url = f"http://127.0.0.1:{port}"
    config = default_server_config()
    config.auth.mode = "dev"
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.server.public_base_url = base_url
    app = create_server_app(config)

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("ushell server did not start")
        time.sleep(0.05)

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


class TestUshellE2E:
    async def _create_ushell(self, base_url: str, session_id: str) -> None:
        async with httpx.AsyncClient(base_url=base_url) as http:
            r = await http.post(
                "/api/sessions",
                json={"session_id": session_id, "connector_type": "ushell", "auto_start": True},
            )
            assert r.status_code == 200

    async def _wait_connected(self, base_url: str, session_id: str) -> None:
        async with httpx.AsyncClient(base_url=base_url) as http:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                resp = await http.get(f"/api/sessions/{session_id}")
                if resp.status_code == 200 and resp.json()["connected"] is True:
                    return
                await asyncio.sleep(0.1)
        raise AssertionError(f"session did not become connected: {session_id}")

    async def test_ushell_created_via_api(self, ushell_server: str) -> None:
        async with httpx.AsyncClient(base_url=ushell_server) as http:
            r = await http.post(
                "/api/sessions",
                json={"session_id": "ushell-create", "connector_type": "ushell", "auto_start": True},
            )
        assert r.status_code == 200
        assert r.json()["connector_type"] == "ushell"

    async def test_ushell_browser_receives_hello_open_mode(self, ushell_server: str) -> None:
        await self._create_ushell(ushell_server, "ushell-hello")
        await self._wait_connected(ushell_server, "ushell-hello")
        async with websockets.connect(_ws_url(ushell_server, "/ws/browser/ushell-hello/term")) as ws:
            hello = await _drain_until(ws, "hello", timeout=5.0)
        assert hello is not None
        assert hello["worker_online"] is True
        assert hello["input_mode"] == "open"

    async def test_ushell_help_command_returns_command_list(self, ushell_server: str) -> None:
        await self._create_ushell(ushell_server, "ushell-help")
        await self._wait_connected(ushell_server, "ushell-help")
        async with websockets.connect(_ws_url(ushell_server, "/ws/browser/ushell-help/term")) as ws:
            assert await _drain_until(ws, "hello", timeout=5.0) is not None
            await ws.send(encode_data("help\n"))
            output = await _drain_term_until(ws, "ushell commands", timeout=5.0)
        assert "ushell commands" in output

    async def test_ushell_py_command_evaluates_expression(self, ushell_server: str) -> None:
        await self._create_ushell(ushell_server, "ushell-py")
        await self._wait_connected(ushell_server, "ushell-py")
        async with websockets.connect(_ws_url(ushell_server, "/ws/browser/ushell-py/term")) as ws:
            assert await _drain_until(ws, "hello", timeout=5.0) is not None
            await ws.send(encode_data("py 1 + 1\n"))
            output = await _drain_term_until(ws, "2", timeout=5.0)
        assert "2" in output
