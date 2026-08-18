#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared fixtures for telnet e2e scenarios.

All sessions use ``auto_start=True`` backed by a mock asyncio telnet server so
the full stack is exercised:

    Browser WS  →  FastAPI  →  TermHub  →  HostedSessionRuntime
                                                    ↕
                                        TelnetSessionConnector
                                                    ↕
                                        TelnetTransport (TCP)
                                                    ↕
                                        mock asyncio.Server

Key design decisions
--------------------
* Port pre-allocation: ``HostedSessionRuntime._ws_url()`` derives its URL from
  ``config.server.public_base_url`` which is computed at model-creation time
  from host + port.  We must therefore bind to a known port *before* creating
  the config and pass the same port to uvicorn.
* wait_for_session_connected: after the server starts the runtime spins up in a
  background task (0.15 s delay).  Callers must poll the HTTP status endpoint
  until ``connected=True`` before connecting a browser WS — otherwise the hub
  has no ``last_snapshot`` and the browser silently never receives one.
* Raw websockets + ControlStreamDecoder: browser WS messages are control-stream
  encoded.  We use the same manual-decode pattern as ``tests/server/test_app.py``
  rather than ``connect_async_ws``.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Any

import httpx2
import pytest
import uvicorn
import websockets

from provide.uterm.control_channel import ControlChunk, DataChunk
from provide.uterm.control_channel import ControlFrameDecoder as ControlStreamDecoder
from provide.uterm.server.app import create_server_app
from provide.uterm.server.config import config_from_mapping
from provide.uterm.transports.telnet_server import start_telnet_server

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}


# ---------------------------------------------------------------------------
# Mock telnet handlers
# ---------------------------------------------------------------------------


async def _echo_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Send a recognisable banner then echo any received bytes back."""
    writer.write(b"ECHO_BANNER\r\n")
    await writer.drain()
    with contextlib.suppress(asyncio.CancelledError, ConnectionResetError, OSError):
        while True:
            try:
                data = await asyncio.wait_for(reader.read(1024), timeout=0.2)
            except TimeoutError:
                continue
            if not data:
                break
            writer.write(data)
            await writer.drain()


async def _xff_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Send data that contains 0xFF bytes (IAC IAC = literal 0xFF in telnet)."""
    writer.write(b"DATA_START\xff\xffDATA_END\r\n")
    await writer.drain()
    with contextlib.suppress(asyncio.CancelledError, ConnectionResetError, OSError):
        await asyncio.sleep(30.0)


# ---------------------------------------------------------------------------
# Port helper
# ---------------------------------------------------------------------------


def _alloc_port() -> int:
    """Return a free ephemeral TCP port by briefly binding to it.

    There is a small TOCTOU window between releasing the port here and uvicorn
    binding to it.  This is acceptable for tests — the probability of collision
    is negligible in a local test run, and retrying on bind failure would
    complicate the fixture considerably.
    """
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Core fixture builder
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _live_server_cm(sessions: list[dict[str, Any]]) -> Any:
    """Spin up a uterm server with the given auto-start telnet sessions."""
    uterm_port = _alloc_port()

    # Explicitly set public_base_url — _merged_config_mapping starts from
    # default_server_config().model_dump() which has public_base_url already
    # set to the default port.  The model validator only overwrites it when the
    # field is empty, so we must pass the correct URL explicitly.
    cfg = config_from_mapping(
        {
            "server": {
                "host": "127.0.0.1",
                "port": uterm_port,
                "public_base_url": f"http://127.0.0.1:{uterm_port}",
            },
            "auth": {
                "mode": "header",
                "header_mode_acknowledged": True,
                "worker_bearer_token": "test-bearer-token-32-chars-long-x",
            },
            "sessions": sessions,
        }
    )
    app = create_server_app(cfg)
    config = uvicorn.Config(app, host="127.0.0.1", port=uterm_port, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10.0
    while not server.started:
        if loop.time() > deadline:
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=2.0)
            raise RuntimeError("live_telnet_server: uvicorn startup timeout")
        await asyncio.sleep(0.05)

    base_url = f"http://127.0.0.1:{uterm_port}"
    try:
        yield base_url
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def live_telnet_server() -> Any:
    """Single auto-start telnet session (echo handler). Yields (base_url, mock_srv)."""
    srv = await start_telnet_server(_echo_handler, "127.0.0.1", 0, negotiation_delay_s=0.05)
    telnet_port = srv.sockets[0].getsockname()[1]

    try:
        async with _live_server_cm(
            [
                {
                    "session_id": "tel1",
                    "display_name": "Telnet Session",
                    "connector_type": "telnet",
                    "auto_start": True,
                    "connector_config": {"host": "127.0.0.1", "port": telnet_port},
                }
            ]
        ) as base_url:
            yield base_url, srv
    finally:
        srv.close()


@pytest.fixture()
async def live_two_telnet_server() -> Any:
    """Two auto-start telnet sessions with separate echo handlers. Yields base_url."""
    srv1 = await start_telnet_server(_echo_handler, "127.0.0.1", 0, negotiation_delay_s=0.05)
    srv2 = await start_telnet_server(_echo_handler, "127.0.0.1", 0, negotiation_delay_s=0.05)
    port1 = srv1.sockets[0].getsockname()[1]
    port2 = srv2.sockets[0].getsockname()[1]

    try:
        async with _live_server_cm(
            [
                {
                    "session_id": "tel1",
                    "display_name": "Telnet One",
                    "connector_type": "telnet",
                    "auto_start": True,
                    "connector_config": {"host": "127.0.0.1", "port": port1},
                },
                {
                    "session_id": "tel2",
                    "display_name": "Telnet Two",
                    "connector_type": "telnet",
                    "auto_start": True,
                    "connector_config": {"host": "127.0.0.1", "port": port2},
                },
            ]
        ) as base_url:
            yield base_url
    finally:
        srv1.close()
        srv2.close()


@pytest.fixture()
async def live_xff_telnet_server() -> Any:
    """Single auto-start telnet session with 0xFF-emitting handler. Yields base_url."""
    srv = await start_telnet_server(_xff_handler, "127.0.0.1", 0, negotiation_delay_s=0.05)
    telnet_port = srv.sockets[0].getsockname()[1]

    try:
        async with _live_server_cm(
            [
                {
                    "session_id": "tel1",
                    "display_name": "xff Session",
                    "connector_type": "telnet",
                    "auto_start": True,
                    "connector_config": {"host": "127.0.0.1", "port": telnet_port},
                }
            ]
        ) as base_url:
            yield base_url
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


async def wait_for_session_connected(base_url: str, session_id: str, timeout: float = 15.0) -> None:
    """Poll the HTTP API until the session runtime reports connected=True.

    Must be called before connecting a browser WS to ensure ``last_snapshot``
    is populated — otherwise the browser may never receive an initial snapshot.
    """
    async with httpx2.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=5.0) as http:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            resp = await http.get(f"/api/sessions/{session_id}")
            if resp.status_code == 200 and resp.json().get("connected") is True:
                return
            await asyncio.sleep(0.1)
    raise AssertionError(f"session {session_id!r} did not become connected within {timeout}s")


# Per-WS decoder/pending state — module-level so helpers can share state
# across multiple drain calls on the same connection.
_WS_DECODERS: dict[Any, ControlStreamDecoder] = {}
_WS_PENDING: dict[Any, list[dict[str, Any]]] = {}


async def _drain_until(ws: Any, type_: str, timeout: float = 5.0) -> dict[str, Any] | None:
    """Drain a raw websockets WS until a control frame with the given type arrives.

    Uses manual ControlStreamDecoder decoding — the same approach as
    ``tests/server/test_app.py::_drain_until``.
    """
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
            decoder = _WS_DECODERS.setdefault(ws, ControlStreamDecoder())
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


async def drain_for_snapshot(ws: Any, timeout: float = 5.0) -> dict[str, Any] | None:
    """Drain until any snapshot frame is received."""
    return await _drain_until(ws, "snapshot", timeout=timeout)


async def drain_for_snapshot_with_text(
    ws: Any,
    text: str,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """Drain until a snapshot whose screen contains *text* is received."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        snap = await _drain_until(ws, "snapshot", timeout=min(0.5, remaining))
        # A 0.5s slice with no snapshot just means the worker hasn't produced
        # one yet — keep waiting until the outer deadline rather than giving
        # up on the first idle window.
        if snap is None:
            continue
        if text in snap.get("screen", ""):
            return snap
    return None


@contextlib.asynccontextmanager
async def connect_browser(base_url: str, session_id: str) -> Any:
    """Open a raw websockets browser WS connection with admin headers."""
    url = ws_url(base_url, f"/ws/browser/{session_id}/term")
    async with websockets.connect(url, additional_headers=ADMIN_H) as ws:
        yield ws
