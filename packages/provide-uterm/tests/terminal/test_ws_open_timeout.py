# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public session/factory opening policy and a real bounded stalled upgrade."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from provide.uterm.ws_session import WebSocketSession, connect_ws
from websockets.asyncio.server import serve
from websockets.protocol import State


@pytest.mark.parametrize("factory", [False, True])
@pytest.mark.parametrize("budget", [30.0, None, 10.0])
async def test_public_opening_deadline_reaches_client(monkeypatch, factory, budget):
    entry = connect_ws if factory else WebSocketSession
    assert "open_timeout" in inspect.signature(entry).parameters, "Expose opening policy without changing keepalive"

    async def receive():
        await asyncio.Event().wait()

    peer = SimpleNamespace(state=State.OPEN, recv=AsyncMock(side_effect=receive), close=AsyncMock())
    opening = AsyncMock(return_value=peer)
    monkeypatch.setattr("websockets.connect", opening)
    session = None
    try:
        if factory:
            session = await connect_ws("ws://offline.invalid", open_timeout=budget)
        else:
            session = WebSocketSession("ws://offline.invalid", open_timeout=budget)
            await session.connect()
        if budget == 10.0:
            assert "open_timeout" not in opening.call_args.kwargs
        else:
            assert opening.call_args.kwargs["open_timeout"] == budget
        assert session.is_connected()
    finally:
        if session is not None:
            await asyncio.wait_for(session.close(), 1)


async def test_stalled_local_upgrade_still_expires_with_original_timeout():
    assert "open_timeout" in inspect.signature(connect_ws).parameters
    entered, release = asyncio.Event(), asyncio.Event()

    async def stalled(_connection, _request):
        entered.set()
        await release.wait()

    async def handler(_connection):
        return None

    async with serve(handler, "127.0.0.1", 0, process_request=stalled) as server:
        port = server.sockets[0].getsockname()[1]
        task = asyncio.create_task(connect_ws(f"ws://127.0.0.1:{port}", open_timeout=0.05))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            with pytest.raises(ConnectionError) as caught:
                await asyncio.wait_for(task, 1)
            assert isinstance(caught.value.__cause__, TimeoutError)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("entry", [connect_ws, WebSocketSession])
def test_ordinary_client_keeps_ten_second_default(entry):
    assert inspect.signature(entry).parameters["open_timeout"].default == 10.0


@pytest.mark.parametrize("entry", [connect_ws, WebSocketSession])
def test_redirect_refusal_is_optional_and_preserves_ordinary_clients(entry):
    assert "follow_redirects" in inspect.signature(entry).parameters
    assert inspect.signature(entry).parameters["follow_redirects"].default is True


async def test_session_threads_redirect_refusal_without_starting_reader():
    assert "follow_redirects" in inspect.signature(WebSocketSession).parameters
    session = WebSocketSession("wss://offline.invalid/ws", follow_redirects=False)
    transport = SimpleNamespace(connect=AsyncMock())
    session._transport = transport
    await session._connect_transport()
    assert transport.connect.call_args.kwargs["follow_redirects"] is False


async def test_factory_threads_redirect_refusal(monkeypatch):
    monkeypatch.setattr(WebSocketSession, "connect", AsyncMock())
    session = await connect_ws("wss://offline.invalid/ws", follow_redirects=False)
    transport = SimpleNamespace(connect=AsyncMock())
    session._transport = transport
    await session._connect_transport()
    assert transport.connect.call_args.kwargs["follow_redirects"] is False


@pytest.mark.parametrize("factory", [False, True])
async def test_default_opening_controls_preserve_legacy_transport_kwargs(monkeypatch, factory):
    monkeypatch.setattr(WebSocketSession, "connect", AsyncMock())
    session = await connect_ws("ws://offline.invalid") if factory else WebSocketSession("ws://offline.invalid")
    transport = SimpleNamespace(connect=AsyncMock())
    session._transport = transport
    await session._connect_transport()
    assert "open_timeout" not in transport.connect.call_args.kwargs
    assert "follow_redirects" not in transport.connect.call_args.kwargs
