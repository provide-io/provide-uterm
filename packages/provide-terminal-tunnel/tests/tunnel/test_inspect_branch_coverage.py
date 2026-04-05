#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted branch coverage tests for inspect.py — closes remaining branch gaps."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from contextlib import suppress
from typing import Any

import httpx
import pytest
import uvicorn
import websockets.server

from provide.terminal.cli.inspect import _run_inspect
from provide.terminal.tunnel.protocol import CHANNEL_HTTP, decode_frame, encode_frame

# ---------------------------------------------------------------------------
# Shared helpers (reuse pattern from test_run_inspect_integration.py)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            await w.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise TimeoutError(f"port {port} did not open within {timeout}s")


class _TunnelWS:
    """Minimal mock tunnel WS server."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.frames: list[dict[str, Any]] = []
        self._ws: Any = None
        self._server: Any = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        self._server = await websockets.server.serve(self._handler, "127.0.0.1", self.port)

    async def _handler(self, ws: Any) -> None:
        self._ws = ws
        self._ready.set()
        try:
            async for raw in ws:
                if isinstance(raw, bytes) and len(raw) > 2:
                    frame = decode_frame(raw)
                    if frame.channel == CHANNEL_HTTP:
                        with suppress(Exception):
                            self.frames.append(json.loads(frame.payload))
        except Exception:
            pass

    async def wait_ready(self) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=5.0)

    async def wait_for_frame(self, frame_type: str, timeout: float = 5.0) -> dict[str, Any]:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            for f in self.frames:
                if f.get("type") == frame_type:
                    self.frames.remove(f)
                    return f
            await asyncio.sleep(0.05)
        raise TimeoutError(f"no {frame_type} frame within {timeout}s")

    async def send_action(self, msg: dict[str, Any]) -> None:
        payload = json.dumps(msg).encode()
        await self._ws.send(encode_frame(CHANNEL_HTTP, payload))

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
async def target_server():
    port = _free_port()

    async def _app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        body_parts: list[bytes] = []
        while True:
            msg = await receive()
            body_parts.append(msg.get("body", b""))
            if not msg.get("more_body", False):
                break
        resp = json.dumps({"echo": True, "body": b"".join(body_parts).decode(errors="replace")}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": resp})

    config = uvicorn.Config(_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await _wait_for_port(port)
    yield port
    server.should_exit = True
    await task


@pytest.fixture
async def tunnel_ws():
    port = _free_port()
    ws = _TunnelWS(port)
    await ws.start()
    yield ws
    await ws.stop()


async def _start_inspect(
    target_port: int, ws_port: int, proxy_port: int, *, intercept: bool = False
) -> asyncio.Task[None]:
    task = asyncio.create_task(
        _run_inspect(
            f"ws://127.0.0.1:{ws_port}",
            "",
            target_port,
            proxy_port,
            intercept=intercept,
            intercept_timeout=2.0,
            intercept_timeout_action="forward",
        )
    )
    await _wait_for_port(proxy_port)
    return task


@pytest.fixture
def proxy_port() -> int:
    return _free_port()


# ---------------------------------------------------------------------------
# Branch coverage tests
# ---------------------------------------------------------------------------


class TestInspectBranchCoverage:
    """Close the 4 remaining branch misses in inspect.py."""

    @pytest.mark.timeout(15)
    async def test_no_intercept_message_when_disabled(
        self, target_server: int, tunnel_ws: _TunnelWS, proxy_port: int
    ) -> None:
        """Branch 123->125: intercept=False skips intercept config print."""
        task = await _start_inspect(target_server, tunnel_ws.port, proxy_port, intercept=False)
        await tunnel_ws.wait_ready()
        state = await tunnel_ws.wait_for_frame("http_intercept_state")
        assert state["enabled"] is False
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @pytest.mark.timeout(15)
    async def test_multi_chunk_request_body(
        self, target_server: int, tunnel_ws: _TunnelWS, proxy_port: int
    ) -> None:
        """Branch 210->207: more_body=True causes loop to continue."""
        task = await _start_inspect(target_server, tunnel_ws.port, proxy_port)
        await tunnel_ws.wait_ready()
        # Large body — uvicorn/ASGI may deliver in multiple chunks
        async with httpx.AsyncClient() as client:
            resp = await asyncio.wait_for(
                client.post(f"http://127.0.0.1:{proxy_port}/chunked", content=b"x" * 65536),
                timeout=5.0,
            )
        assert resp.status_code == 200
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @pytest.mark.timeout(15)
    async def test_modify_body_only_no_headers(
        self, target_server: int, tunnel_ws: _TunnelWS, proxy_port: int
    ) -> None:
        """Branch 273->275: modify with body_b64 but no headers key."""
        task = await _start_inspect(target_server, tunnel_ws.port, proxy_port, intercept=True)
        await tunnel_ws.wait_ready()
        await tunnel_ws.wait_for_frame("http_intercept_state")

        async def _do_request() -> httpx.Response:
            async with httpx.AsyncClient() as client:
                return await client.get(f"http://127.0.0.1:{proxy_port}/body-mod")

        req_task = asyncio.create_task(_do_request())
        req_frame = await tunnel_ws.wait_for_frame("http_req")
        # Modify with body but NO headers — exercises 273->275 false branch
        await tunnel_ws.send_action({
            "type": "http_action",
            "id": req_frame["id"],
            "action": "modify",
            "body_b64": base64.b64encode(b"modified body").decode(),
        })
        resp = await asyncio.wait_for(req_task, timeout=5.0)
        assert resp.status_code == 200
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @pytest.mark.timeout(15)
    async def test_inspect_toggle_loops_back(
        self, target_server: int, tunnel_ws: _TunnelWS, proxy_port: int
    ) -> None:
        """Branch 356->325: http_inspect_toggle loops back to WS receive."""
        task = await _start_inspect(target_server, tunnel_ws.port, proxy_port)
        await tunnel_ws.wait_ready()
        await tunnel_ws.wait_for_frame("http_intercept_state")
        await tunnel_ws.send_action({"type": "http_inspect_toggle", "enabled": False})
        await asyncio.sleep(0.2)
        await tunnel_ws.send_action({"type": "http_inspect_toggle", "enabled": True})
        await asyncio.sleep(0.2)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
