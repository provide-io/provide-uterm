#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for entry.py — Default.fetch() dispatch logic."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from provide_terminal_cloudflare.cf_types import Response
from provide_terminal_cloudflare.entry import Default, _extract_worker_id

# ---------------------------------------------------------------------------
# _extract_worker_id
# ---------------------------------------------------------------------------


def test_extract_worker_id_ws_browser() -> None:
    assert _extract_worker_id("/ws/browser/my-session/term") == "my-session"


def test_extract_worker_id_ws_worker() -> None:
    assert _extract_worker_id("/ws/worker/sess-123/term") == "sess-123"


def test_extract_worker_id_ws_raw() -> None:
    assert _extract_worker_id("/ws/raw/raw-sess/term") == "raw-sess"


def test_extract_worker_id_worker_hijack() -> None:
    assert _extract_worker_id("/worker/abc/hijack/acquire") == "abc"


def test_extract_worker_id_worker_input_mode() -> None:
    assert _extract_worker_id("/worker/abc/input_mode") == "abc"


def test_extract_worker_id_unknown() -> None:
    assert _extract_worker_id("/api/health") is None


def test_extract_worker_id_root() -> None:
    assert _extract_worker_id("/") is None


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------


def _make_default(env_attrs: dict | None = None) -> Default:
    """Create a Default instance with a minimal env."""
    attrs: dict = {"AUTH_MODE": "dev"}
    if env_attrs:
        attrs.update(env_attrs)
    return Default(SimpleNamespace(**attrs))


def _req(path: str) -> SimpleNamespace:
    return SimpleNamespace(url=f"https://x{path}")


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------


async def test_default_fetch_health() -> None:
    """Lines 43-50: /api/health → ok=True."""
    d = _make_default()
    resp = await d.fetch(_req("/api/health"))
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["ok"] is True and "provide-terminal" in data["service"]


# ---------------------------------------------------------------------------
# /api/sessions
# ---------------------------------------------------------------------------


async def test_default_fetch_sessions_no_kv() -> None:
    """Lines 52-58: no SESSION_REGISTRY → scope='local'."""
    d = _make_default()
    with patch("provide_terminal_cloudflare.entry.list_kv_sessions", new=AsyncMock(return_value=[])):
        resp = await d.fetch(_req("/api/sessions"))
    assert resp.status == 200
    assert resp.headers.get("X-Sessions-Scope") == "local"  # type: ignore[union-attr]


async def test_default_fetch_sessions_with_kv() -> None:
    """Lines 55-58: SESSION_REGISTRY present → scope='fleet'."""
    d = _make_default({"SESSION_REGISTRY": object()})
    with patch("provide_terminal_cloudflare.entry.list_kv_sessions", new=AsyncMock(return_value=[])):
        resp = await d.fetch(_req("/api/sessions"))
    assert resp.headers.get("X-Sessions-Scope") == "fleet"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Asset paths
# ---------------------------------------------------------------------------


async def test_default_fetch_assets_prefix_path() -> None:
    """Lines 60-61: /assets/... → serve_asset."""
    d = _make_default()
    mock_resp = Response(body="<html>", status=200)
    with patch("provide_terminal_cloudflare.entry.serve_asset", return_value=mock_resp):
        resp = await d.fetch(_req("/assets/terminal.html"))
    assert resp.status == 200


async def test_default_fetch_static_js_path() -> None:
    """Lines 62-63: /hijack.js → serve_asset."""
    d = _make_default()
    mock_resp = Response(body="js", status=200)
    with patch("provide_terminal_cloudflare.entry.serve_asset", return_value=mock_resp) as mock_sa:
        await d.fetch(_req("/hijack.js"))
    mock_sa.assert_called_once_with("hijack.js")


async def test_default_fetch_static_html_path() -> None:
    """Lines 62-63: /terminal.html → serve_asset."""
    d = _make_default()
    mock_resp = Response(body="html", status=200)
    with patch("provide_terminal_cloudflare.entry.serve_asset", return_value=mock_resp):
        resp = await d.fetch(_req("/terminal.html"))
    assert resp.status == 200


# ---------------------------------------------------------------------------
# Worker routes
# ---------------------------------------------------------------------------


async def test_default_fetch_worker_route_no_binding_returns_500() -> None:
    """Lines 73-75: SESSION_RUNTIME binding missing → 500."""
    d = _make_default()
    resp = await d.fetch(_req("/ws/worker/my-id/term"))
    assert resp.status == 500


async def test_default_fetch_worker_route_with_binding_calls_stub() -> None:
    """Lines 77-79: SESSION_RUNTIME present → stub.fetch called."""
    mock_resp = Response(body='{"ok":true}', status=200)

    async def stub_fetch(req: object) -> Response:
        return mock_resp

    stub = SimpleNamespace(fetch=stub_fetch)
    ns = SimpleNamespace(idFromName=lambda wid: "sid", get=lambda sid: stub)
    d = _make_default({"SESSION_RUNTIME": ns})
    resp = await d.fetch(_req("/ws/worker/my-id/term"))
    assert resp.status == 200


# ---------------------------------------------------------------------------
# Special paths when no worker_id extracted
# ---------------------------------------------------------------------------


async def test_default_fetch_app_path() -> None:
    """/app → read_asset_text('terminal.html') with <base href="/assets/"> injected."""
    d = _make_default()
    with patch("provide_terminal_cloudflare.entry.read_asset_text", return_value="<head><title>T</title>") as mock_rat:
        resp = await d.fetch(_req("/app"))
    mock_rat.assert_called_once_with("terminal.html")
    assert resp.status == 200
    assert '<base href="/assets/">' in str(resp.body)


async def test_default_fetch_app_slash_path() -> None:
    """/app/ → same base-tag injection as /app."""
    d = _make_default()
    with patch("provide_terminal_cloudflare.entry.read_asset_text", return_value="<head><title>T</title>") as mock_rat:
        resp = await d.fetch(_req("/app/"))
    mock_rat.assert_called_once_with("terminal.html")
    assert resp.status == 200


async def test_default_fetch_app_path_fallback() -> None:
    """/app falls back to serve_asset when read_asset_text returns None."""
    d = _make_default()
    mock_resp = Response(body="terminal", status=200)
    with (
        patch("provide_terminal_cloudflare.entry.read_asset_text", return_value=None),
        patch("provide_terminal_cloudflare.entry.serve_asset", return_value=mock_resp) as mock_sa,
    ):
        resp = await d.fetch(_req("/app"))
    mock_sa.assert_called_once_with("terminal.html")
    assert resp.status == 200


async def test_default_fetch_root_path() -> None:
    """Lines 69-70: / → serve_asset('hijack.html')."""
    d = _make_default()
    mock_resp = Response(body="hijack", status=200)
    with patch("provide_terminal_cloudflare.entry.serve_asset", return_value=mock_resp) as mock_sa:
        await d.fetch(_req("/"))
    mock_sa.assert_called_once_with("hijack.html")


async def test_default_fetch_unknown_path_returns_404() -> None:
    """Line 71: unknown path → 404."""
    d = _make_default()
    resp = await d.fetch(_req("/unknown-endpoint"))
    assert resp.status == 404
    data = json.loads(resp.body)
    assert data["error"] == "not_found"


# ---------------------------------------------------------------------------
# Config caching
# ---------------------------------------------------------------------------


async def test_default_fetch_caches_config_across_requests() -> None:
    """Lines 33-39: second fetch reuses cached _config (same object)."""
    d = _make_default()
    await d.fetch(_req("/api/health"))
    config_first = d._config  # type: ignore[attr-defined]
    await d.fetch(_req("/api/health"))
    assert d._config is config_first  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# /api/sessions — JWT auth in jwt mode (lines 55-71)
# ---------------------------------------------------------------------------


import time

import jwt as _jwt


def _make_token(sub: str = "user") -> str:
    now = int(time.time())
    return _jwt.encode({"sub": sub, "exp": now + 600}, "test-secret", algorithm="HS256")


def _make_jwt_default() -> Default:
    """Default instance configured with jwt mode."""
    return Default(
        SimpleNamespace(
            AUTH_MODE="jwt",
            JWT_ALGORITHMS="HS256",
            JWT_PUBLIC_KEY_PEM="test-secret",
            WORKER_BEARER_TOKEN="test-worker-token",
        )
    )


async def test_sessions_jwt_mode_no_auth_header_returns_401() -> None:
    """Line 60: no Authorization header in jwt mode → 401."""
    d = _make_jwt_default()
    r = SimpleNamespace(url="https://x/api/sessions", headers=SimpleNamespace(get=lambda k, default=None: None))
    resp = await d.fetch(r)
    assert resp.status == 401
    data = json.loads(resp.body)
    assert data["error"] == "authentication required"


async def test_sessions_jwt_mode_non_bearer_returns_401() -> None:
    """Line 60: Authorization header without 'Bearer ' prefix → 401."""
    d = _make_jwt_default()
    r = SimpleNamespace(
        url="https://x/api/sessions",
        headers=SimpleNamespace(get=lambda k, default=None: "Basic abc123"),
    )
    resp = await d.fetch(r)
    assert resp.status == 401


async def test_sessions_jwt_mode_empty_token_returns_401() -> None:
    """Line 63: Authorization: Bearer (empty) → 401."""
    d = _make_jwt_default()
    r = SimpleNamespace(
        url="https://x/api/sessions",
        headers=SimpleNamespace(get=lambda k, default=None: "Bearer "),
    )
    resp = await d.fetch(r)
    assert resp.status == 401


async def test_sessions_jwt_mode_invalid_token_returns_401() -> None:
    """Line 71: invalid token → 401 with error=invalid token."""
    d = _make_jwt_default()
    r = SimpleNamespace(
        url="https://x/api/sessions",
        headers=SimpleNamespace(get=lambda k, default=None: "Bearer not.a.valid.token"),
    )
    resp = await d.fetch(r)
    assert resp.status == 401
    data = json.loads(resp.body)
    assert data["error"] == "invalid token"


async def test_sessions_jwt_mode_valid_token_returns_200() -> None:
    """Lines 68-77: valid token in jwt mode → 200 with sessions list."""
    d = _make_jwt_default()
    token = _make_token()
    r = SimpleNamespace(
        url="https://x/api/sessions",
        headers=SimpleNamespace(get=lambda k, default=None: f"Bearer {token}"),
    )
    with patch("provide_terminal_cloudflare.entry.list_kv_sessions", new=AsyncMock(return_value=[])):
        resp = await d.fetch(r)
    assert resp.status == 200


async def test_sessions_jwt_mode_headers_get_raises_returns_401() -> None:
    """Lines 57-58: headers.get() raises → auth_header falls back to '' → 401."""
    d = _make_jwt_default()

    class _BadHeaders:
        def get(self, k, default=None):
            raise RuntimeError("headers error")

    r = SimpleNamespace(url="https://x/api/sessions", headers=_BadHeaders())
    resp = await d.fetch(r)
    assert resp.status == 401


async def test_sessions_jwt_mode_cookie_token_returns_200() -> None:
    """_extract_bearer_or_cookie: CF_Authorization cookie path → valid token accepted."""
    d = _make_jwt_default()
    token = _make_token()

    def _get_header(k, default=None):
        if k == "Cookie":
            return f"session=abc; CF_Authorization={token}; other=x"
        return None

    r = SimpleNamespace(url="https://x/api/sessions", headers=SimpleNamespace(get=_get_header))
    with patch("provide_terminal_cloudflare.entry.list_kv_sessions", new=AsyncMock(return_value=[])):
        resp = await d.fetch(r)
    assert resp.status == 200
