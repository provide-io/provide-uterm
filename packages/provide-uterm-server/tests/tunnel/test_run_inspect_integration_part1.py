#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for _run_inspect — real WS + HTTP proxy, no mocks."""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import suppress
from typing import Any

import pytest
import uvicorn
import websockets.server

from provide.uterm.cli.inspect import _run_inspect
from provide.uterm.tunnel.protocol import (
    CHANNEL_HTTP,
    decode_frame,
    encode_frame,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    """Poll until a TCP port is accepting connections."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            await w.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    msg = f"port {port} did not open within {timeout}s"
    raise TimeoutError(msg)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def target_server():
    """Tiny ASGI echo server on an ephemeral port."""
    port = _free_port()

    async def _echo_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        body_parts: list[bytes] = []
        while True:
            msg = await receive()
            body_parts.append(msg.get("body", b""))
            if not msg.get("more_body", False):
                break
        req_body = b"".join(body_parts)
        method = scope["method"]
        path = scope["path"]
        qs = scope.get("query_string", b"").decode()
        resp = json.dumps(
            {
                "echo": True,
                "method": method,
                "path": path,
                "qs": qs,
                "body": req_body.decode(errors="replace"),
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(resp)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": resp})

    config = uvicorn.Config(_echo_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await _wait_for_port(port)
    yield port
    server.should_exit = True
    await task


class TunnelWSServer:
    """Mock tunnel WS server that records frames and can send actions."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.frames: list[dict[str, Any]] = []
        self._ws: websockets.server.ServerConnection | None = None
        self._server: websockets.server.WebSocketServer | None = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        self._server = await websockets.server.serve(
            self._handler,
            "127.0.0.1",
            self.port,
        )
        self._ready.set()

    async def _handler(self, ws: websockets.server.ServerConnection) -> None:
        self._ws = ws
        try:
            async for raw in ws:
                if isinstance(raw, bytes) and len(raw) > 2:
                    frame = decode_frame(raw)
                    if frame.channel == CHANNEL_HTTP:
                        with suppress(Exception):
                            self.frames.append(json.loads(frame.payload))
                    # Also store non-HTTP frames (e.g. CHANNEL_DATA) for completeness
                    elif frame.channel != CHANNEL_HTTP:
                        # control messages etc — just skip
                        pass
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send_action(self, msg: dict[str, Any]) -> None:
        """Send an http_action frame as binary tunnel frame."""
        assert self._ws is not None
        payload = json.dumps(msg).encode()
        await self._ws.send(encode_frame(CHANNEL_HTTP, payload))

    async def send_text_action(self, msg: dict[str, Any]) -> None:
        """Send an action as a text frame (FastAPI relay path)."""
        assert self._ws is not None
        await self._ws.send(json.dumps(msg))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def frames_of_type(self, t: str) -> list[dict[str, Any]]:
        return [f for f in self.frames if f.get("type") == t]

    async def wait_for_frame(self, frame_type: str, timeout: float = 5.0) -> dict[str, Any]:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            matches = self.frames_of_type(frame_type)
            if matches:
                return matches[-1]
            await asyncio.sleep(0.05)
        msg = f"no {frame_type} frame within {timeout}s"
        raise TimeoutError(msg)

    async def wait_for_n_frames(self, frame_type: str, n: int, timeout: float = 5.0) -> list[dict[str, Any]]:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            matches = self.frames_of_type(frame_type)
            if len(matches) >= n:
                return matches[:n]
            await asyncio.sleep(0.05)
        got = len(self.frames_of_type(frame_type))
        msg = f"only {got}/{n} {frame_type} frames within {timeout}s"
        raise TimeoutError(msg)


@pytest.fixture
async def mock_tunnel_ws():
    port = _free_port()
    server = TunnelWSServer(port)
    await server.start()
    yield server
    await server.stop()


_TEST_TOKEN = "test-token"


@pytest.fixture
async def inspect_proxy(target_server: int, mock_tunnel_ws: TunnelWSServer):
    """Start _run_inspect as a background task, yield proxy port."""
    proxy_port = _free_port()
    ws_endpoint = f"ws://127.0.0.1:{mock_tunnel_ws.port}"

    task = asyncio.create_task(
        _run_inspect(
            ws_endpoint=ws_endpoint,
            worker_token=_TEST_TOKEN,
            target_port=target_server,
            listen_port=proxy_port,
        )
    )
    await _wait_for_port(proxy_port)
    yield proxy_port
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@pytest.fixture
async def inspect_proxy_intercept(target_server: int, mock_tunnel_ws: TunnelWSServer):
    """Start _run_inspect with intercept=True."""
    proxy_port = _free_port()
    ws_endpoint = f"ws://127.0.0.1:{mock_tunnel_ws.port}"

    task = asyncio.create_task(
        _run_inspect(
            ws_endpoint=ws_endpoint,
            worker_token=_TEST_TOKEN,
            target_port=target_server,
            listen_port=proxy_port,
            intercept=True,
            intercept_timeout=3.0,
            intercept_timeout_action="forward",
        )
    )
    await _wait_for_port(proxy_port)
    yield proxy_port
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
