#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for manager/auth.py TokenAuthMiddleware."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

from provide.terminal.manager.auth import TokenAuthMiddleware, setup_auth


def _make_mw(bearer: str = "secret", **kwargs) -> TokenAuthMiddleware:  # type: ignore[return]
    inner = AsyncMock()
    mw = TokenAuthMiddleware(inner, bearer, **kwargs)
    mw._inner = inner  # type: ignore[attr-defined]
    return mw


# ---------------------------------------------------------------------------
# scope_type detection — non-http/websocket passes through with correct args
# ---------------------------------------------------------------------------


class TestScopeTypePassthrough:
    async def test_lifespan_passes_scope_not_none(self) -> None:
        """mut_10/11/12/13/14/15: pass None/wrong args for non-http scopes."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "tok")
        scope = {"type": "lifespan"}
        receive = AsyncMock()
        send = AsyncMock()
        await mw(scope, receive, send)
        # inner must be called with exactly the original scope, receive, send
        inner.assert_awaited_once_with(scope, receive, send)

    async def test_lifespan_passes_correct_receive(self) -> None:
        """mut_11: receive=None passed to inner."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "tok")
        scope = {"type": "lifespan"}
        receive = AsyncMock()
        send = AsyncMock()
        await mw(scope, receive, send)
        call_args = inner.call_args
        assert call_args[0][1] is receive  # second positional arg is receive

    async def test_lifespan_passes_correct_send(self) -> None:
        """mut_12: send=None passed to inner."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "tok")
        scope = {"type": "lifespan"}
        receive = AsyncMock()
        send = AsyncMock()
        await mw(scope, receive, send)
        call_args = inner.call_args
        assert call_args[0][2] is send  # third positional arg is send


# ---------------------------------------------------------------------------
# scope_type "type" key
# ---------------------------------------------------------------------------


class TestScopeTypeKey:
    async def test_type_key_is_string_not_none(self) -> None:
        """mut_2/3/4: get("type") key changed to None/'XXtypeXX'/'TYPE'."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        # http type with valid bearer — should be accepted
        scope = {
            "type": "http",
            "path": "/api",
            "method": "GET",
            "headers": [(b"authorization", b"Bearer secret")],
        }
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    async def test_http_type_checked_not_websocket(self) -> None:
        """mut_5: 'not in' → 'in' flips logic."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        # Non-http/websocket passes through WITHOUT auth
        scope = {"type": "lifespan"}
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    async def test_websocket_type_requires_auth(self) -> None:
        """mut_8/9: websocket removed/uppercased from allowed types."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        receive = AsyncMock(return_value={"type": "websocket.connect"})
        sent = []

        async def fake_send(msg):
            sent.append(msg)

        scope = {"type": "websocket", "path": "/ws/test", "query_string": b"token=bad"}
        await mw(scope, receive, fake_send)
        inner.assert_not_awaited()
        assert any(m.get("type") == "websocket.close" for m in sent)


# ---------------------------------------------------------------------------
# path key
# ---------------------------------------------------------------------------


class TestPathKey:
    async def test_path_default_empty_string(self) -> None:
        """mut_18/20/23: path default=None/'XXXX' — no-path scope should not crash."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        # Scope without path key — uses default
        scope = {
            "type": "http",
            "method": "GET",
            "headers": [(b"authorization", b"Bearer secret")],
        }
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    async def test_path_key_is_path_not_other(self) -> None:
        """mut_21/22: 'path' key changed to 'XXpathXX'/'PATH'."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret", public_paths=frozenset({"/health"}))
        scope = {
            "type": "http",
            "path": "/health",
            "method": "GET",
            "headers": [],
        }
        # /health is public — should pass through
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    async def test_path_in_public_paths_passes_without_auth(self) -> None:
        """mut_24/25: 'or' → 'and', 'in' → 'not in' for public_paths check."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret", public_paths=frozenset({"/dashboard"}))
        # No auth header — but path is public, should still pass through
        scope = {"type": "http", "path": "/dashboard", "method": "GET", "headers": []}
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    async def test_path_matching_public_prefix_passes(self) -> None:
        """mut_26/27: any(None) or startswith(None) would fail."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret", public_prefixes=("/static/",))
        scope = {"type": "http", "path": "/static/main.js", "method": "GET", "headers": []}
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    async def test_public_path_passes_correct_scope(self) -> None:
        """mut_28: scope=None passed to inner from public path branch."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret", public_paths=frozenset({"/health"}))
        scope = {"type": "http", "path": "/health", "method": "GET", "headers": []}
        await mw(scope, AsyncMock(), AsyncMock())
        # Must be called with the actual scope, not None
        call_args = inner.call_args
        assert call_args[0][0] is scope


# ---------------------------------------------------------------------------
# websocket branch
# ---------------------------------------------------------------------------


class TestWebsocketBranch:
    async def test_scope_type_websocket_uses_query_string(self) -> None:
        """mut_34/35/36: 'websocket' key mutated."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "mytoken")
        scope = {"type": "websocket", "path": "/ws/x", "query_string": b"token=mytoken"}
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    async def test_websocket_token_in_query_string(self) -> None:
        """mut_45: query_string default removed."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "mytoken")
        # Scope without query_string key — should use default b""
        scope = {"type": "websocket", "path": "/ws/x"}
        receive = AsyncMock(return_value={"type": "websocket.connect"})
        sent = []

        async def fake_send(msg):
            sent.append(msg)

        await mw(scope, receive, fake_send)
        # No token → rejected
        inner.assert_not_awaited()
        assert any(m.get("type") == "websocket.close" for m in sent)

    async def test_websocket_token_from_query(self) -> None:
        """mut_60: [0] → [1] — token at index 0 not 1."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "mytoken")
        # ?token=mytoken — parse_qs gives {'token': ['mytoken']}, index 0
        scope = {"type": "websocket", "path": "/ws/x", "query_string": b"token=mytoken"}
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    async def test_websocket_close_code_4403_on_rejection(self) -> None:
        """Verify close code is 4403 not something else."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "mytoken")
        scope = {"type": "websocket", "path": "/ws/x", "query_string": b"token=bad"}
        receive = AsyncMock(return_value={"type": "websocket.connect"})
        sent = []

        async def fake_send(msg):
            sent.append(msg)

        await mw(scope, receive, fake_send)
        close_msg = next(m for m in sent if m.get("type") == "websocket.close")
        assert close_msg["code"] == 4403


# ---------------------------------------------------------------------------
# HTTP branch — headers key
# ---------------------------------------------------------------------------


class TestHttpBranch:
    async def test_headers_key_is_headers_not_null(self) -> None:
        """mut_80: scope.get("headers") → scope.get(None)."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        scope = {
            "type": "http",
            "path": "/api",
            "method": "GET",
            "headers": [(b"authorization", b"Bearer secret")],
        }
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    async def test_options_method_passes_without_auth(self) -> None:
        """mut_70: 'OPTIONS' → 'XXOPTIONSXX'."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        scope = {"type": "http", "path": "/api", "method": "OPTIONS", "headers": []}
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    async def test_non_options_method_requires_auth(self) -> None:
        """Verify OPTIONS bypass doesn't apply to DELETE."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        scope = {
            "type": "http",
            "path": "/api",
            "method": "DELETE",
            "headers": [],
        }
        sent = []

        async def fake_send(msg):
            sent.append(msg)

        await mw(scope, AsyncMock(), fake_send)
        inner.assert_not_awaited()

    async def test_x_api_token_header_accepted(self) -> None:
        """Verify x-api-token header path works."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        scope = {
            "type": "http",
            "path": "/api",
            "method": "GET",
            "headers": [(b"x-api-token", b"secret")],
        }
        await mw(scope, AsyncMock(), AsyncMock())
        inner.assert_awaited_once()

    async def test_unauthorized_http_returns_401(self) -> None:
        """Verify 401 response status on bad token."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        scope = {
            "type": "http",
            "path": "/api",
            "method": "GET",
            "headers": [(b"authorization", b"Bearer wrong")],
        }
        sent = []

        async def fake_send(msg):
            sent.append(msg)

        await mw(scope, AsyncMock(), fake_send)
        inner.assert_not_awaited()
        status_msg = next((m for m in sent if m.get("type") == "http.response.start"), None)
        assert status_msg is not None
        assert status_msg["status"] == 401


# ---------------------------------------------------------------------------
# setup_auth
# ---------------------------------------------------------------------------


class TestSetupAuth:
    def test_token_env_var_wires_middleware(self) -> None:
        """Verify env var is read and middleware added."""
        app = MagicMock()
        with patch.dict(os.environ, {"TEST_TOKEN_VAR": "mytoken"}):
            setup_auth(app, env_var="TEST_TOKEN_VAR")
        app.add_middleware.assert_called_once_with(
            TokenAuthMiddleware,
            token="mytoken",
            public_paths=frozenset(),
            public_prefixes=(),
        )

    def test_whitespace_only_token_skips_auth(self) -> None:
        """Whitespace-only env var = no token = no middleware."""
        app = MagicMock()
        with patch.dict(os.environ, {"TEST_TOKEN_VAR": "   "}):
            setup_auth(app, env_var="TEST_TOKEN_VAR")
        app.add_middleware.assert_not_called()

    def test_config_public_paths_passed_to_middleware(self) -> None:
        """Config public_paths/prefixes are forwarded."""
        app = MagicMock()
        config = MagicMock()
        config.auth_public_paths = ["/health", "/ready"]
        config.auth_public_prefixes = ["/public/"]
        with patch.dict(os.environ, {"TEST_TOKEN_VAR": "tok"}):
            setup_auth(app, env_var="TEST_TOKEN_VAR", config=config)
        call = app.add_middleware.call_args
        assert call.kwargs["public_paths"] == frozenset({"/health", "/ready"})
        assert call.kwargs["public_prefixes"] == ("/public/",)
