#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.manager.auth."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.manager.auth import TokenAuthMiddleware, setup_auth


class TestTokenAuthMiddleware:
    @pytest.fixture
    def middleware(self):
        app = AsyncMock()
        return TokenAuthMiddleware(
            app,
            "secret123",
            public_paths=frozenset({"/", "/dashboard"}),
            public_prefixes=("/static/",),
        )

    @pytest.mark.asyncio
    async def test_non_http_passes_through(self):
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "tok")
        scope = {"type": "lifespan"}
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_public_path_passes_through(self):
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "tok", public_paths=frozenset({"/dashboard"}))
        scope = {"type": "http", "path": "/dashboard", "method": "GET"}
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_public_prefix_passes_through(self):
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "tok", public_prefixes=("/static/",))
        scope = {"type": "http", "path": "/static/dashboard.js", "method": "GET"}
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_options_passes_through(self):
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "tok")
        scope = {"type": "http", "path": "/api/status", "method": "OPTIONS", "headers": []}
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bearer_token_accepted(self):
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        scope = {
            "type": "http",
            "path": "/api/bots",
            "method": "GET",
            "headers": [(b"authorization", b"Bearer secret")],
        }
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_x_api_token_accepted(self):
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        scope = {
            "type": "http",
            "path": "/api/bots",
            "method": "GET",
            "headers": [(b"x-api-token", b"secret")],
        }
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bad_token_rejected_http(self):
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        scope = {
            "type": "http",
            "path": "/api/bots",
            "method": "GET",
            "headers": [(b"authorization", b"Bearer wrong")],
        }
        # Capture what gets sent back
        sent = []

        async def fake_send(msg):
            sent.append(msg)

        # JSONResponse is called internally — we just check inner was NOT called
        await mw(scope, AsyncMock(), fake_send)
        inner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_websocket_token_accepted(self):
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "wstok")
        scope = {
            "type": "websocket",
            "path": "/ws/swarm",
            "query_string": b"token=wstok",
        }
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_websocket_bad_token_rejected(self):
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "wstok")
        scope = {
            "type": "websocket",
            "path": "/ws/swarm",
            "query_string": b"token=bad",
        }
        receive = AsyncMock(return_value={"type": "websocket.connect"})
        sent = []

        async def fake_send(msg):
            sent.append(msg)

        await mw(scope, receive, fake_send)
        inner.assert_not_awaited()
        assert any(m.get("type") == "websocket.close" for m in sent)

    @pytest.mark.asyncio
    async def test_no_auth_header_rejected(self):
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        scope = {
            "type": "http",
            "path": "/api/bots",
            "method": "GET",
            "headers": [],
        }
        sent = []

        async def fake_send(msg):
            sent.append(msg)

        await mw(scope, AsyncMock(), fake_send)
        inner.assert_not_awaited()


class TestSetupAuth:
    def test_no_token_skips(self):
        app = MagicMock()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UTERM_MANAGER_API_TOKEN", None)
            setup_auth(app, env_var="UTERM_MANAGER_API_TOKEN")
        app.add_middleware.assert_not_called()

    def test_with_token_adds_middleware(self):
        app = MagicMock()
        with patch.dict(os.environ, {"UTERM_MANAGER_API_TOKEN": "mytoken"}):
            setup_auth(app, env_var="UTERM_MANAGER_API_TOKEN")
        app.add_middleware.assert_called_once()

    def test_with_config(self):
        app = MagicMock()
        config = MagicMock()
        config.auth_public_paths = ["/dashboard"]
        config.auth_public_prefixes = ["/static/"]
        with patch.dict(os.environ, {"MY_TOK": "val"}):
            setup_auth(app, env_var="MY_TOK", config=config)
        app.add_middleware.assert_called_once()
