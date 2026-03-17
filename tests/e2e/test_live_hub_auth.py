#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""E2E worker token authentication tests."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import uvicorn
import websockets
from fastapi import FastAPI
from websockets.exceptions import ConnectionClosedError

from provide.terminal.hijack.hub import TermHub

from .conftest import _wait_for_server, _ws_url


@asynccontextmanager
async def _hub_with_worker_token(token: str | None = None):
    """Context manager: hub with optional worker token."""
    hub = TermHub(
        resolve_browser_role=lambda _ws, _worker_id: "admin",
        worker_token=token,
    )
    app = FastAPI()
    app.include_router(hub.create_router())

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    try:
        await _wait_for_server(server, task, "auth_hub")
        port: int = server.servers[0].sockets[0].getsockname()[1]
        yield hub, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)


class TestWorkerTokenAuth:
    """Worker token authentication on WS /ws/worker/{id}/term endpoint."""

    async def test_worker_without_token_gets_1008(self) -> None:
        """Worker connects without Authorization header; connection closed with 1008."""
        async with _hub_with_worker_token(token="secret-token") as (_hub, base_url):
            url = _ws_url(base_url, "/ws/worker/wa1/term")
            try:
                async with websockets.connect(url) as ws:
                    # Attempt to receive; connection should close with 1008
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    # Should not reach here if auth fails properly
                    raise AssertionError(f"Expected connection closure, got message: {msg}")
            except ConnectionClosedError as e:
                # Expect close code 1008 (policy violation)
                # websockets library: rcvd is Close frame with code attribute
                assert hasattr(e, "rcvd") and e.rcvd.code == 1008, (
                    f"Expected close code 1008, got {e.rcvd.code if hasattr(e, 'rcvd') else 'no rcvd'}"
                )

    async def test_worker_wrong_token_gets_1008(self) -> None:
        """Worker connects with wrong token; connection closed with 1008."""
        async with _hub_with_worker_token(token="secret-token") as (_hub, base_url):
            url = _ws_url(base_url, "/ws/worker/wa2/term")
            try:
                async with websockets.connect(url, additional_headers={"Authorization": "Bearer wrong-token"}) as ws:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    raise AssertionError(f"Expected closure, got: {msg}")
            except ConnectionClosedError as e:
                assert hasattr(e, "rcvd") and e.rcvd.code == 1008, (
                    f"Expected close code 1008 for wrong token, got {e.rcvd.code if hasattr(e, 'rcvd') else 'no rcvd'}"
                )

    async def test_worker_correct_token_connects(self) -> None:
        """Worker connects with correct token; connection accepted and receives hello."""
        async with _hub_with_worker_token(token="secret-token") as (_hub, base_url):
            url = _ws_url(base_url, "/ws/worker/wa3/term")
            async with websockets.connect(url, additional_headers={"Authorization": "Bearer secret-token"}) as ws:
                # Should receive snapshot_req message from hub
                msg_str = await asyncio.wait_for(ws.recv(), timeout=1.0)
                msg = json.loads(msg_str)
                # First message should be snapshot_req
                assert msg.get("type") == "snapshot_req", f"Expected snapshot_req, got {msg.get('type')}"

    async def test_no_token_required_when_not_set(self) -> None:
        """Hub without worker_token; worker connects without auth header successfully."""
        async with _hub_with_worker_token(token=None) as (_hub, base_url):
            url = _ws_url(base_url, "/ws/worker/wa4/term")
            async with websockets.connect(url) as ws:
                # Should connect and receive snapshot_req
                msg_str = await asyncio.wait_for(ws.recv(), timeout=1.0)
                msg = json.loads(msg_str)
                assert msg.get("type") == "snapshot_req", f"Expected snapshot_req without auth, got {msg.get('type')}"

    async def test_partial_bearer_prefix_fails(self) -> None:
        """Authorization header with 'Bearer' but no token fails auth."""
        async with _hub_with_worker_token(token="secret-token") as (_hub, base_url):
            url = _ws_url(base_url, "/ws/worker/wa5/term")
            try:
                async with websockets.connect(url, additional_headers={"Authorization": "Bearer"}) as ws:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    raise AssertionError(f"Expected closure, got: {msg}")
            except ConnectionClosedError as e:
                # Empty token after "Bearer " should fail
                assert hasattr(e, "rcvd") and e.rcvd.code == 1008, (
                    f"Expected close code 1008 for empty bearer, got {e.rcvd.code if hasattr(e, 'rcvd') else 'no rcvd'}"
                )
