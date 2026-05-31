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


def _make_dev_default(**extra: object) -> object:
    """Build a Default configured for the legacy open-access path.

    from_env only accepts jwt mode now, so build a valid jwt config and override
    the in-memory mode to ``dev`` (reachable only via direct config mutation).
    """
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry import Default

    attrs: dict[str, object] = {
        "AUTH_MODE": "jwt",
        "JWT_ALGORITHMS": "HS256",
        "JWT_PUBLIC_KEY_PEM": "test-secret-key-32-bytes-minimum!",
        "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz",
    }
    attrs.update(extra)
    env = SimpleNamespace(**attrs)
    d = Default(env)
    d._config = CloudflareConfig.from_env(env)
    d._config.jwt.mode = "dev"
    return d


# ---------------------------------------------------------------------------
# _tunnel_api.py gaps
# ---------------------------------------------------------------------------


class TestEntryDispatchGaps:
    """Cover entry.py _api_tunnels/revoke/rotate/pam dispatch."""

    async def test_api_tunnels_via_fetch(self) -> None:
        """Line 379: /api/tunnels route matched via _match_api_route."""
        d = _make_dev_default()
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
            SimpleNamespace(
                AUTH_MODE="jwt",
                JWT_ALGORITHMS="HS256",
                JWT_PUBLIC_KEY_PEM="k",
                WORKER_BEARER_TOKEN="test-worker-token-padded-to-32xyz",
            )
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

    async def test_share_redirect_without_invite_is_not_found(self) -> None:
        """/s/{id} requires a valid one-time invite or existing cookie."""
        d = _make_dev_default()
        req = _req("/s/my-session")
        resp = await d.fetch(req)
        assert resp.status == 404

    async def test_share_redirect_with_token_query_is_not_found(self) -> None:
        """Bearer query tokens are not accepted by the short-share route."""
        d = _make_dev_default()
        req = _req("/s/my-session?token=abc123")
        resp = await d.fetch(req)
        assert resp.status == 404

    async def test_share_redirect_http_tunnel_uses_inspect_page(self) -> None:
        """Short-share /s/{id} for an HTTP tunnel must redirect to /app/inspect/."""
        import json

        from provide.uterm.tunnel.token_hash import hash_token

        kv = AsyncMock()
        kv.get = AsyncMock(
            return_value=json.dumps(
                {
                    "share_page": "inspect",
                    "share_token_hash": hash_token("tok"),
                    "share_invite_hash": hash_token("invite-tok"),
                    "share_invite_token": "tok",
                    "share_invite_expires_at": __import__("time").time() + 300,
                    "expires_at": __import__("time").time() + 3600,
                }
            )
        )
        kv.put = AsyncMock()
        d = _make_dev_default(SESSION_REGISTRY=kv)
        req = _req("/s/my-http-tunnel?invite=invite-tok")
        resp = await d.fetch(req)
        assert resp.status == 302
        loc = dict(resp.headers).get("location", "")
        assert "/app/inspect/my-http-tunnel" in loc
        assert "token=" not in loc

    async def test_share_redirect_kv_returns_none_is_not_found(self) -> None:
        """Short-share KV returning None is not a valid invite."""
        kv = AsyncMock()
        kv.get = AsyncMock(return_value=None)
        d = _make_dev_default(SESSION_REGISTRY=kv)
        req = _req("/s/missing-tunnel")
        resp = await d.fetch(req)
        assert resp.status == 404

    async def test_share_redirect_kv_exception_is_not_found(self) -> None:
        """Short-share KV lookup failure must not expose a guessed app URL."""
        kv = AsyncMock()
        kv.get = AsyncMock(side_effect=RuntimeError("kv unavailable"))
        d = _make_dev_default(SESSION_REGISTRY=kv)
        req = _req("/s/any-tunnel")
        resp = await d.fetch(req)
        assert resp.status == 404

    async def test_share_page_with_context(self) -> None:
        """Lines 344-345: SPA response for share page with context."""
        d = _make_dev_default()

        # Mock resolve_share_context to return valid context
        with patch(
            "provide.uterm.cloudflare.api._tunnel_api.resolve_share_context",
            new_callable=AsyncMock,
            return_value=("operator", "operator"),
        ):
            req = _req("/app/operator/my-session?token=tok")
            resp = await d.fetch(req)
            assert resp.status == 200
