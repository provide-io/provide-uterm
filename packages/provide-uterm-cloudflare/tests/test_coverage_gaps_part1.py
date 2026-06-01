#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests targeting remaining coverage gaps in CF package."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import provide.uterm.cloudflare.cf_types  # noqa: F401

from provide.uterm.tunnel.token_hash import hash_token

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _req(
    path: str,
    method: str = "GET",
    headers: dict | None = None,
    body: object = None,
) -> SimpleNamespace:
    hdr = headers or {}

    def _get(k: str, default: object = None) -> object:
        return hdr.get(k, default)

    async def _json() -> object:
        if body is None:
            raise ValueError("no body")
        return body

    return SimpleNamespace(
        url=f"https://x{path}",
        method=method,
        headers=SimpleNamespace(get=_get),
        json=_json,
    )


# ---------------------------------------------------------------------------
# _tunnel_api.py gaps
# ---------------------------------------------------------------------------


class TestTunnelApiGaps:
    """Cover _tunnel_api.py missing lines."""

    async def test_json_parse_error_defaults_to_empty(self) -> None:
        """Lines 29-30: body defaults to {} on JSON parse error."""
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnels

        req = _req("/api/tunnels", method="POST")
        # json() raises → body should default to {}
        env = SimpleNamespace(SESSION_REGISTRY=None)
        resp = await handle_tunnels(req, env)
        assert resp.status in {200, 400, 405}

    async def test_resolve_session_corrupt_json(self) -> None:
        """Lines 154-155: corrupt KV entry returns None via resolve_share_context."""
        from provide.uterm.cloudflare.api._tunnel_api import (
            resolve_share_context,
        )

        kv = AsyncMock()
        kv.get = AsyncMock(return_value="not-valid-json{{{")
        env = SimpleNamespace(SESSION_REGISTRY=kv)
        result = await resolve_share_context(
            _req("/app/session/test-id?token=abc"),
            env,
            "test-id",
        )
        assert result is None

    async def test_resolve_share_context_no_token(self) -> None:
        """Lines 164-165, 176-182: no token in query or cookies."""
        from provide.uterm.cloudflare.api._tunnel_api import (
            resolve_share_context,
        )

        req = _req("/app/session/test-id")
        env = SimpleNamespace(SESSION_REGISTRY=None)
        result = await resolve_share_context(req, env, "test-id")
        assert result is None

    async def test_resolve_share_url_parse_exception(self) -> None:
        """Lines 164-165: URL parse exception silently caught."""
        import json as _json

        from provide.uterm.cloudflare.api._tunnel_api import (
            resolve_share_context,
        )

        kv = AsyncMock()
        kv.get = AsyncMock(
            return_value=_json.dumps({"share_token_hash": hash_token("tok")}),
        )
        env = SimpleNamespace(SESSION_REGISTRY=kv)

        class _BadUrl:
            def __str__(self) -> str:
                raise RuntimeError("broken url")

        req = SimpleNamespace(
            url=_BadUrl(),
            method="GET",
            headers=SimpleNamespace(get=lambda k, d=None: d),
        )
        result = await resolve_share_context(req, env, "test-id")
        assert result is None

    async def test_resolve_share_cookie_parse_exception(self) -> None:
        """Lines 178-179: cookie parse exception silently caught."""
        import json as _json

        from provide.uterm.cloudflare.api._tunnel_api import (
            resolve_share_context,
        )

        kv = AsyncMock()
        kv.get = AsyncMock(
            return_value=_json.dumps({"share_token_hash": hash_token("tok")}),
        )
        env = SimpleNamespace(SESSION_REGISTRY=kv)
        req = _req("/app/session/test-id")  # no token in URL
        # Patch SimpleCookie to raise
        with patch(
            "http.cookies.SimpleCookie",
            side_effect=RuntimeError("cookie parse fail"),
        ):
            result = await resolve_share_context(req, env, "test-id")
        assert result is None

    async def test_handle_share_route_valid_context(self) -> None:
        """Lines 213-216: share page rendered with valid context."""
        from provide.uterm.cloudflare.api._tunnel_api import (
            handle_share_route,
        )

        mock_spa = MagicMock()
        mock_spa.return_value = SimpleNamespace(status=200)

        with patch(
            "provide.uterm.cloudflare.api._tunnel_api.resolve_share_context",
            new_callable=AsyncMock,
            return_value=("operator", "operator"),
        ):
            req = _req("/app/operator/test-id?token=abc")
            env = SimpleNamespace(SESSION_REGISTRY=None)
            resp = await handle_share_route(req, env, "test-id", mock_spa)
        assert resp.status == 200
        mock_spa.assert_called_once()

    async def test_resolve_share_context_cookie_parse_error(
        self,
    ) -> None:
        """Lines 178-179: SimpleCookie parse error silently ignored."""
        from provide.uterm.cloudflare.api._tunnel_api import (
            resolve_share_context,
        )

        req = _req(
            "/app/session/test-id",
            headers={"cookie": "\x00invalid\x00cookie"},
        )
        env = SimpleNamespace(SESSION_REGISTRY=None)
        result = await resolve_share_context(req, env, "test-id")
        assert result is None


# ---------------------------------------------------------------------------
# session_runtime.py gaps
# ---------------------------------------------------------------------------
