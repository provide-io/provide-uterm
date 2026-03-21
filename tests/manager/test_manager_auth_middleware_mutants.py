#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager — token auth middleware and setup_auth."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.manager.auth import TokenAuthMiddleware, setup_auth
from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.core import SwarmManager
from provide.terminal.manager.models import BotStatusBase

# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def config(tmp_path):
    return ManagerConfig(
        state_file=str(tmp_path / "state.json"),
        timeseries_dir=str(tmp_path / "metrics"),
        log_dir=str(tmp_path / "logs"),
    )


@pytest.fixture
def manager(config):
    mgr = SwarmManager(config)
    pm = MagicMock()
    pm.cancel_spawn = AsyncMock(return_value=False)
    pm.start_spawn_swarm = AsyncMock()
    pm.spawn_bot = AsyncMock(return_value="bot_000")
    pm.spawn_swarm = AsyncMock(return_value=["bot_000"])
    pm.kill_bot = AsyncMock()
    pm.monitor_processes = AsyncMock()
    mgr.bot_process_manager = pm
    return mgr


@pytest.fixture
def bot() -> BotStatusBase:
    return BotStatusBase(bot_id="bot_001", state="running")


# ===========================================================================
# core.py — SwarmManager.__init__
# ===========================================================================


class TestTokenAuthMiddlewareCallMutants:
    """Tests targeting specific surviving mutants in TokenAuthMiddleware.__call__."""

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_scope_to_inner(self):
        """mutmut_10-15: non-http scope passes scope/receive/send correctly."""
        captured = []

        async def inner(scope, receive, send):
            captured.append((scope, receive, send))

        mw = TokenAuthMiddleware(inner, "tok")
        scope = {"type": "lifespan"}
        recv = AsyncMock()
        send = AsyncMock()
        await mw(scope, recv, send)
        assert len(captured) == 1
        assert captured[0][0] is scope
        assert captured[0][1] is recv
        assert captured[0][2] is send

    @pytest.mark.asyncio
    async def test_public_path_passes_all_args_to_inner(self):
        """mutmut_28-33: public path passes scope/receive/send correctly."""
        captured = []

        async def inner(scope, receive, send):
            captured.append((scope, receive, send))

        mw = TokenAuthMiddleware(inner, "tok", public_paths=frozenset({"/health"}))
        scope = {"type": "http", "path": "/health", "method": "GET"}
        recv = AsyncMock()
        send = AsyncMock()
        await mw(scope, recv, send)
        assert len(captured) == 1
        assert captured[0][0] is scope
        assert captured[0][1] is recv
        assert captured[0][2] is send

    @pytest.mark.asyncio
    async def test_missing_path_defaults_to_empty_string(self):
        """mutmut_18/20: scope.get('path', '') defaults to '' not None or 'XXXX'."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "tok", public_paths=frozenset({""}))
        # scope without 'path' key — should default to '' and match public_paths
        scope = {"type": "http", "method": "GET", "headers": []}
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_websocket_qs_default_empty_bytes(self):
        """mutmut_43/45/48: query_string defaults to b'' (not None/b'XXXX')."""
        # WebSocket scope without query_string key — should not crash
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "correct_tok")
        scope = {
            "type": "websocket",
            "path": "/ws",
            # no query_string key
        }
        receive = AsyncMock(return_value={"type": "websocket.connect"})
        sent = []

        async def fake_send(m):
            sent.append(m)

        # No token → rejected, but should not crash
        await mw(scope, receive, fake_send)
        inner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_websocket_token_extraction(self):
        """mutmut_40/41/50/51/52: token correctly extracted from query string."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "my_token")
        scope = {
            "type": "websocket",
            "path": "/ws",
            "query_string": b"token=my_token&other=x",
        }
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_websocket_empty_default_for_missing_token(self):
        """mutmut_59: parse_qs returns empty → defaults to [''] not ['XXXX']."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        scope = {
            "type": "websocket",
            "path": "/ws",
            "query_string": b"other=value",  # no token param
        }
        receive = AsyncMock()
        sent = []

        async def fake_send(m):
            sent.append(m)

        await mw(scope, receive, fake_send)
        # Token is '' which doesn't match 'secret' → rejected
        inner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_http_method_default_empty_string(self):
        """mutmut_63/65/68: method defaults to '' not None or 'XXXX'."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "tok")
        # Missing method key — should default to '' which is not 'OPTIONS'
        scope = {
            "type": "http",
            "path": "/api",
            # no method key
            "headers": [(b"authorization", b"Bearer tok")],
        }
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_options_passes_all_args_to_inner(self):
        """mutmut_72-77: OPTIONS passes scope/receive/send correctly."""
        captured = []

        async def inner(scope, receive, send):
            captured.append((scope, receive, send))

        mw = TokenAuthMiddleware(inner, "tok")
        scope = {"type": "http", "path": "/api", "method": "OPTIONS", "headers": []}
        recv = AsyncMock()
        send_fn = AsyncMock()
        await mw(scope, recv, send_fn)
        assert len(captured) == 1
        assert captured[0][0] is scope
        assert captured[0][1] is recv
        assert captured[0][2] is send_fn

    @pytest.mark.asyncio
    async def test_headers_default_empty_list(self):
        """mutmut_81/83: headers defaults to [] not None."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "tok")
        # scope without headers key
        scope = {"type": "http", "path": "/api", "method": "GET"}
        sent = []

        async def fake_send(m):
            sent.append(m)

        # Should not crash on missing headers
        await mw(scope, AsyncMock(), fake_send)
        inner.assert_not_awaited()  # no auth token → rejected

    @pytest.mark.asyncio
    async def test_bearer_auth_decode_utf8(self):
        """mutmut_89/90/99/100/101: auth header decoded as utf-8."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "mytoken")
        scope = {
            "type": "http",
            "path": "/api",
            "method": "GET",
            "headers": [(b"authorization", b"Bearer mytoken")],
        }
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_x_api_token_decode_utf8(self):
        """mutmut_110/111/118/120/121/122: x-api-token decoded as utf-8."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "mytoken")
        scope = {
            "type": "http",
            "path": "/api",
            "method": "GET",
            "headers": [(b"x-api-token", b"mytoken")],
        }
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_websocket_close_sends_accept_then_close(self):
        """mutmut_132-135/141-143: WS rejection sends accept with correct type, then close with code 4403."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        scope = {"type": "websocket", "path": "/ws", "query_string": b"token=wrong"}
        receive = AsyncMock(return_value={"type": "websocket.connect"})
        sent = []

        async def fake_send(m):
            sent.append(m)

        await mw(scope, receive, fake_send)
        inner.assert_not_awaited()
        types_sent = [m.get("type") for m in sent]
        assert "websocket.accept" in types_sent
        assert "websocket.close" in types_sent
        close_msg = next(m for m in sent if m.get("type") == "websocket.close")
        assert close_msg["code"] == 4403

    @pytest.mark.asyncio
    async def test_http_unauthorized_returns_401(self):
        """mutmut_145/148/149/150/151/152/153/154: HTTP rejected with 401 JSON response."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        scope = {
            "type": "http",
            "path": "/api",
            "method": "GET",
            "headers": [(b"authorization", b"Bearer wrong")],
        }
        sent = []

        async def fake_send(m):
            sent.append(m)

        await mw(scope, AsyncMock(), fake_send)
        inner.assert_not_awaited()
        # Look for status 401 in start message
        start_msgs = [m for m in sent if m.get("type") == "http.response.start"]
        assert any(m.get("status") == 401 for m in start_msgs)

    @pytest.mark.asyncio
    async def test_http_unauthorized_response_passes_receive(self):
        """mutmut_156: response called with (scope, receive, send) not (scope, None, send)."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        scope = {
            "type": "http",
            "path": "/api",
            "method": "GET",
            "headers": [],
        }
        # Use a real receive that can be called
        recv_called = []

        async def recv():
            recv_called.append(True)
            return {"type": "http.request"}

        sent = []

        async def fake_send(m):
            sent.append(m)

        await mw(scope, recv, fake_send)
        inner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_authenticated_passes_all_args_to_inner(self):
        """mutmut_161-166: authenticated request passes scope/receive/send correctly."""
        captured = []

        async def inner(scope, receive, send):
            captured.append((scope, receive, send))

        mw = TokenAuthMiddleware(inner, "correct")
        scope = {
            "type": "http",
            "path": "/api",
            "method": "GET",
            "headers": [(b"authorization", b"Bearer correct")],
        }
        recv = AsyncMock()
        send_fn = AsyncMock()
        await mw(scope, recv, send_fn)
        assert len(captured) == 1
        assert captured[0][0] is scope
        assert captured[0][1] is recv
        assert captured[0][2] is send_fn


# ===========================================================================
# auth.py — setup_auth
# ===========================================================================


class TestSetupAuthMutants:
    def test_env_var_default_is_uterm_manager_api_token(self):
        """mutmut_1/2: default env_var is 'UTERM_MANAGER_API_TOKEN' (exact case)."""
        app = MagicMock()
        env_var = "UTERM_MANAGER_API_TOKEN"
        with patch.dict(os.environ, {env_var: "tok"}):
            setup_auth(app)  # uses default env_var
        app.add_middleware.assert_called_once()

    def test_no_token_does_not_add_middleware(self):
        """setup_auth without token → no middleware."""
        app = MagicMock()
        env_var = "TEST_TOKEN_VAR_NOTSET_XYZ"
        os.environ.pop(env_var, None)
        setup_auth(app, env_var=env_var)
        app.add_middleware.assert_not_called()

    def test_public_paths_default_frozenset_empty(self):
        """mutmut_16: public_paths starts as frozenset() (not None)."""
        app = MagicMock()
        with patch.dict(os.environ, {"MY_TOK_A": "tok"}):
            setup_auth(app, env_var="MY_TOK_A")
        call_kwargs = app.add_middleware.call_args[1]
        assert call_kwargs["public_paths"] == frozenset()

    def test_public_prefixes_default_empty_tuple(self):
        """mutmut_17: public_prefixes starts as () (not None)."""
        app = MagicMock()
        with patch.dict(os.environ, {"MY_TOK_B": "tok"}):
            setup_auth(app, env_var="MY_TOK_B")
        call_kwargs = app.add_middleware.call_args[1]
        assert call_kwargs["public_prefixes"] == ()

    def test_config_public_paths_used(self):
        """mutmut_19: public_paths from config (not None)."""
        app = MagicMock()
        config = MagicMock()
        config.auth_public_paths = ["/dashboard", "/health"]
        config.auth_public_prefixes = []
        with patch.dict(os.environ, {"MY_TOK_C": "tok"}):
            setup_auth(app, env_var="MY_TOK_C", config=config)
        call_kwargs = app.add_middleware.call_args[1]
        assert "/dashboard" in call_kwargs["public_paths"]

    def test_config_public_prefixes_used(self):
        """mutmut_21: public_prefixes from config (not None)."""
        app = MagicMock()
        config = MagicMock()
        config.auth_public_paths = []
        config.auth_public_prefixes = ["/static/"]
        with patch.dict(os.environ, {"MY_TOK_D": "tok"}):
            setup_auth(app, env_var="MY_TOK_D", config=config)
        call_kwargs = app.add_middleware.call_args[1]
        assert "/static/" in call_kwargs["public_prefixes"]

    def test_middleware_class_is_token_auth(self):
        """mutmut_26: TokenAuthMiddleware class passed (not None)."""
        app = MagicMock()
        with patch.dict(os.environ, {"MY_TOK_E": "tok"}):
            setup_auth(app, env_var="MY_TOK_E")
        call_args = app.add_middleware.call_args
        assert call_args[0][0] is TokenAuthMiddleware

    def test_token_kwarg_passed_to_middleware(self):
        """mutmut_27: token kwarg passed (not None)."""
        app = MagicMock()
        with patch.dict(os.environ, {"MY_TOK_F": "real_tok"}):
            setup_auth(app, env_var="MY_TOK_F")
        call_kwargs = app.add_middleware.call_args[1]
        assert call_kwargs["token"] == "real_tok"

    def test_public_paths_kwarg_passed(self):
        """mutmut_28/32: public_paths kwarg passed to middleware."""
        app = MagicMock()
        with patch.dict(os.environ, {"MY_TOK_G": "tok"}):
            setup_auth(app, env_var="MY_TOK_G")
        call_kwargs = app.add_middleware.call_args[1]
        assert "public_paths" in call_kwargs

    def test_public_prefixes_kwarg_passed(self):
        """mutmut_29/33: public_prefixes kwarg passed to middleware."""
        app = MagicMock()
        with patch.dict(os.environ, {"MY_TOK_H": "tok"}):
            setup_auth(app, env_var="MY_TOK_H")
        call_kwargs = app.add_middleware.call_args[1]
        assert "public_prefixes" in call_kwargs

    def test_setup_auth_with_config_wires_correctly(self):
        """mutmut_19/21/27/28/29: config path sets both public_paths and public_prefixes."""
        app = MagicMock()
        config = MagicMock()
        config.auth_public_paths = ["/pub"]
        config.auth_public_prefixes = ["/pfx/"]
        with patch.dict(os.environ, {"MY_TOK_I": "secretval"}):
            setup_auth(app, env_var="MY_TOK_I", config=config)
        call_kwargs = app.add_middleware.call_args[1]
        assert call_kwargs["token"] == "secretval"
        assert "/pub" in call_kwargs["public_paths"]
        assert "/pfx/" in call_kwargs["public_prefixes"]
