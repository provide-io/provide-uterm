#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for entry.py — Default.fetch() dispatch logic."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from provide.uterm.cloudflare.cf_types import Response
from provide.uterm.cloudflare.entry import Default
from provide.uterm.cloudflare.entry.registry import _extract_worker_id
from provide.uterm.tunnel.token_hash import hash_token

# ---------------------------------------------------------------------------
# _extract_worker_id
# ---------------------------------------------------------------------------


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
    assert data["ok"] is True and "provide-uterm" in data["service"]


# ---------------------------------------------------------------------------
# /api/sessions
# ---------------------------------------------------------------------------


async def test_default_fetch_sessions_no_kv() -> None:
    """Lines 52-58: no SESSION_REGISTRY → scope='local'."""
    d = _make_default()
    with patch("provide.uterm.cloudflare.entry.list_kv_sessions", new=AsyncMock(return_value=[])):
        resp = await d.fetch(_req("/api/sessions"))
    assert resp.status == 200
    assert resp.headers.get("X-Sessions-Scope") == "local"  # type: ignore[union-attr]


async def test_default_fetch_sessions_with_kv() -> None:
    """Lines 55-58: SESSION_REGISTRY present → scope='fleet'."""
    d = _make_default({"SESSION_REGISTRY": object()})
    with patch("provide.uterm.cloudflare.entry.list_kv_sessions", new=AsyncMock(return_value=[])):
        resp = await d.fetch(_req("/api/sessions"))
    assert resp.headers.get("X-Sessions-Scope") == "fleet"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Asset paths
# ---------------------------------------------------------------------------


async def test_default_fetch_assets_prefix_path() -> None:
    """Lines 60-61: /assets/... → serve_asset."""
    d = _make_default()
    mock_resp = Response(body="<html>", status=200)
    with patch("provide.uterm.cloudflare.entry.serve_asset", return_value=mock_resp):
        resp = await d.fetch(_req("/assets/terminal.html"))
    assert resp.status == 200


async def test_default_fetch_static_js_path() -> None:
    """Lines 62-63: /hijack.js → serve_asset."""
    d = _make_default()
    mock_resp = Response(body="js", status=200)
    with patch("provide.uterm.cloudflare.entry.serve_asset", return_value=mock_resp) as mock_sa:
        await d.fetch(_req("/hijack.js"))
    mock_sa.assert_called_once_with("hijack.js")


async def test_default_fetch_static_html_path() -> None:
    """Lines 62-63: /terminal.html → serve_asset."""
    d = _make_default()
    mock_resp = Response(body="html", status=200)
    with patch("provide.uterm.cloudflare.entry.serve_asset", return_value=mock_resp):
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
    """/app → SPA dashboard shell."""
    d = _make_default()
    resp = await d.fetch(_req("/app"))
    assert resp.status == 200
    assert "dashboard" in str(resp.body)


async def test_default_fetch_app_slash_path() -> None:
    """/app/ → SPA dashboard shell."""
    d = _make_default()
    resp = await d.fetch(_req("/app/"))
    assert resp.status == 200
    assert "dashboard" in str(resp.body)


async def test_default_fetch_spa_routes() -> None:
    """SPA routes serve correct page_kind in bootstrap JSON."""
    d = _make_default()
    for path, expected_kind in [
        ("/", "dashboard"),
        ("/app/connect", "connect"),
        ("/app/session/test-123", "session"),
        ("/app/operator/test-123", "operator"),
        ("/app/replay/test-123", "replay"),
    ]:
        resp = await d.fetch(_req(path))
        assert resp.status == 200, f"{path} returned {resp.status}"
        assert expected_kind in str(resp.body), f"{path} missing {expected_kind}"


async def test_default_fetch_share_page_keeps_token_out_of_bootstrap() -> None:
    """Share page bootstrap must not expose the share token to JS."""
    import json as _json

    kv = SimpleNamespace(
        get=AsyncMock(
            return_value=_json.dumps(
                {
                    "share_token_hash": hash_token("shared-tok"),
                    "control_token_hash": hash_token("ctrl-tok"),
                    "expires_at": __import__("time").time() + 3600,
                }
            )
        )
    )
    d = _make_default({"SESSION_REGISTRY": kv})
    req = SimpleNamespace(
        url="https://x/app/session/test-123",
        headers=SimpleNamespace(
            get=lambda k, d=None: "uterm_tunnel_test-123=shared-tok" if k in ("cookie", "Cookie") else d
        ),
    )
    resp = await d.fetch(req)
    assert resp.status == 200
    body = str(resp.body)
    assert "shared-tok" not in body
    bootstrap = _json.loads(body.split("id='app-bootstrap'>")[1].split("</script>")[0])  # type: ignore[union-attr]
    assert bootstrap["page_kind"] == "session"
    assert bootstrap["session_id"] == "test-123"
    assert "shared-tok" not in _json.dumps(bootstrap)
    cookie = str(resp.headers.get("set-cookie") or resp.headers.get("Set-Cookie") or "")
    assert "uterm_tunnel_test-123=shared-tok" in cookie
    assert "HttpOnly" in cookie


async def test_default_fetch_share_route() -> None:
    """Shared tunnel links under /s/{id} redirect to /app/session/{id}."""
    entry = {
        "share_token_hash": hash_token("abc"),
        "control_token_hash": hash_token("def"),
        "share_invite_hash": hash_token("invite-abc"),
        "share_invite_token": "abc",
        "share_invite_expires_at": __import__("time").time() + 300,
        "expires_at": __import__("time").time() + 3600,
    }
    kv = SimpleNamespace(
        get=AsyncMock(return_value=json.dumps(entry)),
        put=AsyncMock(),
    )
    d = _make_default({"SESSION_REGISTRY": kv})
    req = SimpleNamespace(
        url="https://x/s/test-123?invite=invite-abc", headers=SimpleNamespace(get=lambda *_a, **_k: None)
    )
    resp = await d.fetch(req)
    assert resp.status == 302
    assert "/app/session/test-123" in str(resp.headers.get("location", ""))
    assert "token=" not in str(resp.headers.get("location", ""))
    assert "uterm_tunnel_test-123=abc" in str(resp.headers.get("Set-Cookie", ""))


async def test_default_fetch_root_path() -> None:
    """/ → SPA dashboard."""
    d = _make_default()
    resp = await d.fetch(_req("/"))
    assert resp.status == 200
    assert "dashboard" in str(resp.body)


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
    return _jwt.encode({"sub": sub, "exp": now + 600}, "uterm-test-secret-32-byte-minimum-key", algorithm="HS256")


def _make_jwt_default() -> Default:
    """Default instance configured with jwt mode."""
    return Default(
        SimpleNamespace(
            AUTH_MODE="jwt",
            JWT_ALGORITHMS="HS256",
            JWT_PUBLIC_KEY_PEM="uterm-test-secret-32-byte-minimum-key",
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
    with patch("provide.uterm.cloudflare.entry.list_kv_sessions", new=AsyncMock(return_value=[])):
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
    with patch("provide.uterm.cloudflare.entry.list_kv_sessions", new=AsyncMock(return_value=[])):
        resp = await d.fetch(r)
    assert resp.status == 200


# ---------------------------------------------------------------------------
# /api/profiles (dev mode)
# ---------------------------------------------------------------------------


class _FakeKV:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def put(self, key: str, value: str) -> None:
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def list(self, prefix: str = "") -> list[dict[str, str]]:
        return [{"name": k} for k in self._data if k.startswith(prefix)]


async def test_default_fetch_profiles_list_dev_mode() -> None:
    """GET /api/profiles in dev mode uses 'dev-user' principal."""
    kv = _FakeKV()
    d = _make_default({"SESSION_REGISTRY": kv})
    resp = await d.fetch(SimpleNamespace(url="https://x/api/profiles", method="GET", headers={}))
    assert resp.status == 200
    assert json.loads(resp.body) == []


async def test_default_fetch_profiles_create_dev_mode() -> None:
    """POST /api/profiles in dev mode creates profile owned by 'dev-user'."""
    kv = _FakeKV()
    d = _make_default({"SESSION_REGISTRY": kv})

    async def _json() -> dict:
        return {"name": "Test", "connector_type": "ssh"}

    req = SimpleNamespace(url="https://x/api/profiles", method="POST", headers={}, json=_json)
    resp = await d.fetch(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["owner"] == "dev-user"
    assert body["name"] == "Test"


async def test_default_fetch_profiles_jwt_mode_no_token() -> None:
    """GET /api/profiles in jwt mode with no token returns 401."""
    kv = _FakeKV()
    d = _make_default(
        {
            "AUTH_MODE": "jwt",
            "JWT_ALGORITHMS": "HS256",
            "JWT_PUBLIC_KEY_PEM": "test-key",
            "WORKER_BEARER_TOKEN": "tok",
            "SESSION_REGISTRY": kv,
        }
    )
    resp = await d.fetch(SimpleNamespace(url="https://x/api/profiles", method="GET", headers={}))
    # JWT mode requires auth — should be 401 (from _require_jwt)
    assert resp.status == 401


async def test_resolve_principal_id_no_token() -> None:
    """_resolve_principal_id with no token returns 'anonymous'."""
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.auth import _resolve_principal_id

    config = CloudflareConfig.from_env(
        SimpleNamespace(AUTH_MODE="jwt", JWT_ALGORITHMS="HS256", JWT_PUBLIC_KEY_PEM="k", WORKER_BEARER_TOKEN="t")
    )
    result = await _resolve_principal_id(SimpleNamespace(headers=SimpleNamespace(get=lambda _k, d=None: d)), config)
    assert result == "anonymous"


async def test_resolve_principal_id_invalid_token() -> None:
    """_resolve_principal_id with invalid JWT returns 'anonymous'."""
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.auth import _resolve_principal_id

    config = CloudflareConfig.from_env(
        SimpleNamespace(AUTH_MODE="jwt", JWT_ALGORITHMS="HS256", JWT_PUBLIC_KEY_PEM="k", WORKER_BEARER_TOKEN="t")
    )
    req = SimpleNamespace(
        headers=SimpleNamespace(get=lambda k, d=None: "Bearer invalid-token" if k.lower() == "authorization" else d)
    )
    result = await _resolve_principal_id(req, config)
    assert result == "anonymous"


async def test_resolve_principal_id_valid_token() -> None:
    """_resolve_principal_id with valid JWT returns subject."""
    import jwt as pyjwt
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.auth import _resolve_principal_id

    secret = "a-sufficiently-long-secret-key-for-hs256"
    token = pyjwt.encode({"sub": "alice", "exp": 9999999999}, secret, algorithm="HS256")
    config = CloudflareConfig.from_env(
        SimpleNamespace(AUTH_MODE="jwt", JWT_ALGORITHMS="HS256", JWT_PUBLIC_KEY_PEM=secret, WORKER_BEARER_TOKEN="t")
    )
    req = SimpleNamespace(
        headers=SimpleNamespace(get=lambda k, d=None: f"Bearer {token}" if k.lower() == "authorization" else d)
    )
    result = await _resolve_principal_id(req, config)
    assert result == "alice"


async def test_resolve_principal_id_cf_access_email_header() -> None:
    """CF Access authenticated-user-email header resolves to the user's email.

    Without this, a JWKS-only or CF-Access deployment that never configured
    ``public_key_pem`` (because CF Access handles auth upstream) would have
    every profile CRUD action attributed to ``anonymous`` — indistinguishable
    across callers and effectively disabling ownership.
    """
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.auth import _resolve_principal_id

    config = CloudflareConfig.from_env(
        SimpleNamespace(AUTH_MODE="jwt", JWT_ALGORITHMS="HS256", JWT_PUBLIC_KEY_PEM="k", WORKER_BEARER_TOKEN="t")
    )
    req = SimpleNamespace(
        headers=SimpleNamespace(
            get=lambda k, d=None: "alice@example.com" if k.lower() == "cf-access-authenticated-user-email" else d
        )
    )
    result = await _resolve_principal_id(req, config)
    assert result == "alice@example.com"
