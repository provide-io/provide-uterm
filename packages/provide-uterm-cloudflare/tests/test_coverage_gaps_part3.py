#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests targeting remaining coverage gaps in CF package."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import provide.uterm.cloudflare.cf_types  # noqa: F401

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


class TestEntryDispatchGaps:
    """Cover entry.py _api_tunnels/revoke/rotate/pam dispatch."""

    async def test_api_tunnels_via_fetch(self) -> None:
        """Line 379: /api/tunnels route matched via _match_api_route."""
        from provide.uterm.cloudflare.entry import Default

        d = Default(SimpleNamespace(AUTH_MODE="dev"))
        req = _req("/api/tunnels", method="GET")
        resp = await d.fetch(req)
        assert resp.status in {200, 405}

    async def test_api_tunnels_dispatch(self) -> None:
        """Lines 413-418: _api_tunnels calls handle_tunnels."""
        from provide.uterm.cloudflare.entry.handlers import _api_tunnels

        req = _req("/api/tunnels", method="GET")
        env = SimpleNamespace(SESSION_REGISTRY=None)
        cfg = MagicMock()
        resp = await _api_tunnels(req, env, cfg)
        assert resp.status in {200, 405}

    async def test_api_tunnel_revoke_dispatch(self) -> None:
        """Lines 422-427: _api_tunnel_revoke calls handler."""
        from provide.uterm.cloudflare.config import CloudflareConfig
        from provide.uterm.cloudflare.entry.handlers import _api_tunnel_revoke

        cfg = CloudflareConfig.from_env(
            SimpleNamespace(AUTH_MODE="dev", JWT_ALGORITHMS="HS256", WORKER_BEARER_TOKEN="t")
        )
        req = _req("/api/tunnels/tid/tokens", method="DELETE")
        env = SimpleNamespace(SESSION_REGISTRY=None)
        resp = await _api_tunnel_revoke(req, env, cfg, "tid")
        assert resp.status in {200, 404, 500}

    async def test_api_tunnel_rotate_dispatch(self) -> None:
        """Lines 431-436: _api_tunnel_rotate calls handler."""
        from provide.uterm.cloudflare.entry.handlers import _api_tunnel_rotate

        cfg = MagicMock()
        cfg.tunnel_token_ttl_s = 3600
        req = _req("/api/tunnels/tid/tokens/rotate", method="POST")
        env = SimpleNamespace(SESSION_REGISTRY=None)
        resp = await _api_tunnel_rotate(req, env, cfg, "tid")
        assert resp.status in {200, 404, 500}

    async def test_share_redirect_without_query_string(self) -> None:
        """Line 337->339: /s/{id} redirects without query string."""
        from provide.uterm.cloudflare.entry import Default

        d = Default(SimpleNamespace(AUTH_MODE="dev"))
        req = _req("/s/my-session")
        resp = await d.fetch(req)
        assert resp.status == 302
        loc = dict(resp.headers).get("location", "")
        assert "my-session" in loc
        assert "?" not in loc  # no query string appended

    async def test_share_redirect_with_query_string(self) -> None:
        """Lines 337-339: /s/{id}?token=x redirects with query."""
        from provide.uterm.cloudflare.entry import Default

        d = Default(SimpleNamespace(AUTH_MODE="dev"))
        req = _req("/s/my-session?token=abc123")
        resp = await d.fetch(req)
        assert resp.status == 302
        loc = dict(resp.headers).get("location", "")
        assert "my-session" in loc
        assert "token=abc123" in loc

    async def test_share_redirect_http_tunnel_uses_inspect_page(self) -> None:
        """Short-share /s/{id} for an HTTP tunnel must redirect to /app/inspect/."""
        import json

        from provide.uterm.cloudflare.entry import Default

        kv = AsyncMock()
        kv.get = AsyncMock(return_value=json.dumps({"share_page": "inspect"}))
        d = Default(SimpleNamespace(AUTH_MODE="dev", SESSION_REGISTRY=kv))
        req = _req("/s/my-http-tunnel?token=tok")
        resp = await d.fetch(req)
        assert resp.status == 302
        loc = dict(resp.headers).get("location", "")
        assert "/app/inspect/my-http-tunnel" in loc

    async def test_share_redirect_kv_returns_none_falls_back_to_session(self) -> None:
        """Short-share KV returning None must fall back to /app/session/."""
        from provide.uterm.cloudflare.entry import Default

        kv = AsyncMock()
        kv.get = AsyncMock(return_value=None)
        d = Default(SimpleNamespace(AUTH_MODE="dev", SESSION_REGISTRY=kv))
        req = _req("/s/missing-tunnel")
        resp = await d.fetch(req)
        assert resp.status == 302
        loc = dict(resp.headers).get("location", "")
        assert "/app/session/missing-tunnel" in loc

    async def test_share_redirect_kv_exception_falls_back_to_session(self) -> None:
        """Short-share KV lookup failure must fall back to /app/session/."""
        from provide.uterm.cloudflare.entry import Default

        kv = AsyncMock()
        kv.get = AsyncMock(side_effect=RuntimeError("kv unavailable"))
        d = Default(SimpleNamespace(AUTH_MODE="dev", SESSION_REGISTRY=kv))
        req = _req("/s/any-tunnel")
        resp = await d.fetch(req)
        assert resp.status == 302
        loc = dict(resp.headers).get("location", "")
        assert "/app/session/any-tunnel" in loc

    async def test_share_page_with_context(self) -> None:
        """Lines 344-345: SPA response for share page with context."""
        from provide.uterm.cloudflare.entry import Default

        d = Default(SimpleNamespace(AUTH_MODE="dev"))

        # Mock resolve_share_context to return valid context
        with patch(
            "provide.uterm.cloudflare.api._tunnel_api.resolve_share_context",
            new_callable=AsyncMock,
            return_value=("operator", "operator"),
        ):
            req = _req("/app/operator/my-session?token=tok")
            resp = await d.fetch(req)
            assert resp.status == 200
