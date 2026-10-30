# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opening deadlines belong to the WebSocket client, separately from keepalive."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from websockets.protocol import State

from provide.uterm.transports.ws_transport import WebSocketTransport


@pytest.mark.parametrize("budget", [30.0, None])
async def test_transport_forwards_explicit_opening_policy(monkeypatch, budget):
    peer = SimpleNamespace(state=State.OPEN, close=AsyncMock())
    opening = AsyncMock(return_value=peer)
    monkeypatch.setattr("websockets.connect", opening)
    transport = WebSocketTransport()
    try:
        await transport.connect("ignored", 0, url="ws://offline.invalid", open_timeout=budget)
        assert "open_timeout" in opening.call_args.kwargs, "An explicit opening policy must not be silently dropped"
        assert opening.call_args.kwargs["open_timeout"] == budget
        assert transport.is_connected()
    finally:
        await transport.disconnect()


@pytest.mark.parametrize("status", [302, 403])
async def test_forbidden_upgrade_redirect_never_opens_second_destination(monkeypatch, status):
    import asyncio

    from websockets.asyncio.server import serve
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus, SecurityError
    from websockets.http11 import Response

    requested = []
    loop = asyncio.get_running_loop()
    original_connect = loop.create_connection

    async def only_loopback(protocol_factory, host, port, **kwargs):
        requested.append((host, port))
        assert host == "127.0.0.1", "Redirect must not initiate an external request"
        return await original_connect(protocol_factory, host, port, **kwargs)

    async def redirect(_connection, _request):
        return Response(status, "Refused", Headers({"Location": "wss://other.invalid/ws"}), body=b"")

    async def handler(_connection):
        return None

    async with serve(handler, "127.0.0.1", 0, process_request=redirect) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(loop, "create_connection", only_loopback)
        transport = WebSocketTransport()
        with pytest.raises(ConnectionError) as caught:
            await asyncio.wait_for(
                transport.connect("ignored", 0, url=f"ws://127.0.0.1:{port}", follow_redirects=False), 1
            )
        refusal = caught.value.__cause__
        if status == 302:
            assert isinstance(refusal, SecurityError)
            refusal = refusal.__cause__
        assert isinstance(refusal, InvalidStatus)
        assert refusal.response.status_code == status
        assert requested == [("127.0.0.1", port)]
        assert not transport.is_connected()


async def test_refusing_redirects_still_accepts_successful_local_upgrade():
    from websockets.asyncio.server import serve

    async def handler(connection):
        await connection.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        transport = WebSocketTransport()
        try:
            await transport.connect("ignored", 0, url=f"ws://127.0.0.1:{port}", follow_redirects=False)
            assert transport.is_connected()
        finally:
            await transport.disconnect()
