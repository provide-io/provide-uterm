#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.manager.auth."""

from __future__ import annotations

import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.manager.auth import TokenAuthMiddleware, _is_loopback_bind, setup_auth


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

    def test_no_token_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """L2: setup_auth must log a warning at WARNING level when no token is configured."""
        app = MagicMock()
        with (
            patch.dict(os.environ, {}, clear=False),
            caplog.at_level(logging.WARNING, logger="provide.uterm.manager.auth"),
        ):
            os.environ.pop("UTERM_MANAGER_API_TOKEN", None)
            setup_auth(app, env_var="UTERM_MANAGER_API_TOKEN")
        assert any(
            "api_token_auth_disabled" in r.message or "UTERM_MANAGER_API_TOKEN" in r.message for r in caplog.records
        ), f"Expected a warning about missing token, got: {[r.message for r in caplog.records]}"

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


class TestSetupAuthBindHostGuard:
    """Token env-var unset must only be tolerated on loopback binds."""

    @pytest.fixture(autouse=True)
    def _clean_env(self):
        # Ensure neither var leaks between tests.
        for var in ("UTERM_MANAGER_API_TOKEN", "UTERM_MANAGER_ALLOW_UNAUTHENTICATED"):
            os.environ.pop(var, None)
        yield
        for var in ("UTERM_MANAGER_API_TOKEN", "UTERM_MANAGER_ALLOW_UNAUTHENTICATED"):
            os.environ.pop(var, None)

    def test_empty_host_is_not_loopback(self) -> None:
        assert _is_loopback_bind(None) is False
        assert _is_loopback_bind("") is False

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
    def test_loopback_bind_without_token_warns_and_skips(self, host: str) -> None:
        app = MagicMock()
        config = MagicMock()
        config.host = host
        config.auth_public_paths = []
        config.auth_public_prefixes = []
        setup_auth(app, env_var="UTERM_MANAGER_API_TOKEN", config=config)
        app.add_middleware.assert_not_called()

    @pytest.mark.parametrize("host", ["0.0.0.0", "0.0.0.1", "10.0.0.5", "192.168.1.10", "example.com"])
    def test_non_loopback_bind_without_token_raises(self, host: str) -> None:
        app = MagicMock()
        config = MagicMock()
        config.host = host
        config.auth_public_paths = []
        config.auth_public_prefixes = []
        with pytest.raises(RuntimeError, match="Manager API token is required"):
            setup_auth(app, env_var="UTERM_MANAGER_API_TOKEN", config=config)
        app.add_middleware.assert_not_called()

    def test_explicit_opt_out_allows_unauthenticated_on_any_host(self) -> None:
        app = MagicMock()
        config = MagicMock()
        config.host = "10.0.0.5"
        config.auth_public_paths = []
        config.auth_public_prefixes = []
        os.environ["UTERM_MANAGER_ALLOW_UNAUTHENTICATED"] = "1"
        setup_auth(app, env_var="UTERM_MANAGER_API_TOKEN", config=config)
        app.add_middleware.assert_not_called()

    def test_token_set_installs_middleware_on_non_loopback(self) -> None:
        app = MagicMock()
        config = MagicMock()
        config.host = "0.0.0.0"
        config.auth_public_paths = []
        config.auth_public_prefixes = []
        os.environ["UTERM_MANAGER_API_TOKEN"] = "sekrit"
        setup_auth(app, env_var="UTERM_MANAGER_API_TOKEN", config=config)
        app.add_middleware.assert_called_once()
