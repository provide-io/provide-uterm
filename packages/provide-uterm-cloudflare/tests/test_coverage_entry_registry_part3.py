#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for entry.py and state/registry.py."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Ensure cf_types fallback classes (Response, WorkerEntrypoint, DurableObject) are
# loaded before entry.py is imported — entry.py's module-level class definition
# ``class Default(WorkerEntrypoint)`` needs WorkerEntrypoint to be non-None.
import provide.uterm.cloudflare.cf_types  # noqa: F401

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_default(env_attrs: dict | None = None):
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry import Default

    # from_env only accepts jwt mode now; build a valid jwt config and override the
    # in-memory mode to ``dev`` unless the caller explicitly requested jwt enforcement.
    attrs: dict = {
        "AUTH_MODE": "jwt",
        "JWT_ALGORITHMS": "HS256",
        "JWT_PUBLIC_KEY_PEM": "test-secret-key-32-bytes-minimum!",
        "WORKER_BEARER_TOKEN": "test-worker-token",
    }
    force_dev = not (env_attrs and env_attrs.get("AUTH_MODE") == "jwt")
    if env_attrs:
        attrs.update(env_attrs)
    env = SimpleNamespace(**attrs)
    d = Default(env)
    d._config = CloudflareConfig.from_env(env)
    if force_dev:
        d._config.jwt.mode = "dev"
    return d


def _req(path: str, method: str = "GET", headers: dict | None = None) -> SimpleNamespace:
    hdr = headers or {}

    def _get(k, default=None):
        return hdr.get(k, default)

    return SimpleNamespace(url=f"https://x{path}", method=method, headers=SimpleNamespace(get=_get))


# ---------------------------------------------------------------------------
# _resolve_spa_route (lines 129-141)
# ---------------------------------------------------------------------------


def test_match_api_route_tunnel_rotate() -> None:
    from provide.uterm.cloudflare.entry.handlers import _match_api_route

    assert (
        _match_api_route(
            "/api/tunnels/tunnel-abc/tokens/rotate", _req("/api/tunnels/tunnel-abc/tokens/rotate", method="POST")
        )
        is not None
    )


# ---------------------------------------------------------------------------
# Default.fetch() integration — exercises _api_connect/_api_sessions wrappers
# ---------------------------------------------------------------------------


async def test_default_fetch_api_connect_post() -> None:
    """POST /api/connect through Default.fetch() exercises _api_connect (line 349)."""
    kv = AsyncMock()
    kv.put = AsyncMock()
    d = _make_default({"SESSION_REGISTRY": kv})

    async def _json():
        return {"connector_type": "telnet", "display_name": "Test"}

    req = SimpleNamespace(
        url="https://x/api/connect",
        method="POST",
        json=_json,
        headers=SimpleNamespace(get=lambda k, default=None: None),
    )
    resp = await d.fetch(req)
    assert resp.status == 200
    assert json.loads(resp.body)["connector_type"] == "telnet"


async def test_default_fetch_session_delete() -> None:
    """DELETE /api/sessions/{id} through Default.fetch()."""
    d = _make_default()
    req = SimpleNamespace(
        url="https://x/api/sessions/test-sess",
        method="DELETE",
        headers=SimpleNamespace(get=lambda k, default=None: None),
    )
    with patch("provide.uterm.cloudflare.entry.delete_kv_session", new=AsyncMock()):
        resp = await d.fetch(req)
    assert resp.status == 200 and json.loads(resp.body)["deleted"] is True


async def test_route_request_cf_service_token_header_does_not_bypass_jwt() -> None:
    """Raw CF Access headers alone must not bypass JWT auth."""
    from provide.uterm.cloudflare.entry import Default

    d = Default(
        SimpleNamespace(
            AUTH_MODE="jwt",
            JWT_ALGORITHMS="HS256",
            JWT_PUBLIC_KEY_PEM="uterm-test-secret-32-byte-minimum-key",
            WORKER_BEARER_TOKEN="tok",
        )
    )

    def _get(k, default=None):
        if k in ("cf-access-client-id", "CF-Access-Client-Id"):
            return "my-client.access"
        return None

    req = SimpleNamespace(url="https://x/api/sessions", method="GET", headers=SimpleNamespace(get=_get))
    with patch("provide.uterm.cloudflare.entry.list_kv_sessions", new=AsyncMock(return_value=[])):
        resp = await d.fetch(req)
    assert resp.status == 401


# ---------------------------------------------------------------------------
# state/registry.py — delete_kv_session (lines 74-87)
# ---------------------------------------------------------------------------


async def test_delete_kv_session_deletes_key() -> None:
    from provide.uterm.cloudflare.state.registry import delete_kv_session

    kv = AsyncMock()
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    await delete_kv_session(env, "my-worker")
    kv.delete.assert_awaited_once_with("session:my-worker")


async def test_delete_kv_session_no_kv_noop() -> None:
    from provide.uterm.cloudflare.state.registry import delete_kv_session

    await delete_kv_session(SimpleNamespace(), "my-worker")


async def test_delete_kv_session_exception_suppressed() -> None:
    from provide.uterm.cloudflare.state.registry import delete_kv_session

    kv = AsyncMock()
    kv.delete.side_effect = RuntimeError("kv error")
    await delete_kv_session(SimpleNamespace(SESSION_REGISTRY=kv), "my-worker")


# ---------------------------------------------------------------------------
# _decode_jwt_principal — JwtValidationError path (entry.py:251-254)
# ---------------------------------------------------------------------------


async def test_decode_jwt_principal_bad_token_returns_none() -> None:
    """_decode_jwt_principal swallows JwtValidationError and returns None."""
    import time

    import jwt as pyjwt
    from provide.uterm.cloudflare.config import CloudflareConfig, JwtConfig
    from provide.uterm.cloudflare.entry.auth import _decode_jwt_principal

    good_key = "test-secret-key-32-bytes-minimum!"
    wrong_key = "wrong-secret-key-32-bytes-minimum"
    now = int(time.time())
    # Token signed with wrong_key — will fail validation against good_key
    token = pyjwt.encode({"sub": "u1", "exp": now + 600}, wrong_key, algorithm="HS256")

    cfg = CloudflareConfig(jwt=JwtConfig(mode="jwt", public_key_pem=good_key, algorithms=("HS256",)))
    req = SimpleNamespace(
        headers=SimpleNamespace(
            # Only return the bearer for the authorization header — other header
            # names (CF Access) should return default so the bearer-fallback path
            # runs and hits JwtValidationError.
            get=lambda k, default=None: f"Bearer {token}" if k.lower() == "authorization" else default
        )
    )
    result = await _decode_jwt_principal(req, cfg)
    assert result is None


async def test_decode_jwt_principal_cf_access_email_returns_viewer_principal() -> None:
    """CF Access authenticated-user-email maps to a viewer-role Principal.

    Regression guard for the service-token/CF-Access principal-collapse
    bug: before this fix, _decode_jwt_principal returned None for a CF
    Access authenticated request, and downstream handlers treated that as
    anonymous open-access.
    """
    from provide.uterm.cloudflare.config import CloudflareConfig, JwtConfig
    from provide.uterm.cloudflare.entry.auth import _decode_jwt_principal

    cfg = CloudflareConfig(jwt=JwtConfig(mode="jwt", public_key_pem="k", algorithms=("HS256",)))
    req = SimpleNamespace(
        headers=SimpleNamespace(
            get=lambda k, default=None: (
                "alice@example.com" if k.lower() == "cf-access-authenticated-user-email" else default
            )
        )
    )
    result = await _decode_jwt_principal(req, cfg)
    assert result is not None
    assert result.subject_id == "alice@example.com"
    assert "viewer" in result.roles


async def test_decode_jwt_principal_cf_access_service_token_header_returns_none() -> None:
    """Raw CF Access service-token headers do not synthesize a principal."""
    from provide.uterm.cloudflare.config import CloudflareConfig, JwtConfig
    from provide.uterm.cloudflare.entry.auth import _decode_jwt_principal

    cfg = CloudflareConfig(jwt=JwtConfig(mode="jwt", public_key_pem="k", algorithms=("HS256",)))
    client_id = "svc123.access"
    req = SimpleNamespace(
        headers=SimpleNamespace(
            get=lambda k, default=None: client_id if k.lower() == "cf-access-client-id" else default
        )
    )
    result = await _decode_jwt_principal(req, cfg)
    assert result is None


# ---------------------------------------------------------------------------
# _handle_session_delete — session not in KV skips auth (entry.py:327->333)
# ---------------------------------------------------------------------------


async def test_handle_session_delete_session_not_in_kv_returns_404() -> None:
    """When session is absent from KV and principal is present, delete fails closed with 404."""
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.handlers import _handle_session_delete

    mock_del = AsyncMock()
    with patch("provide.uterm.cloudflare.entry.delete_kv_session", new=mock_del):
        with patch(
            "provide.uterm.cloudflare.entry.auth._decode_jwt_principal",
            new=AsyncMock(return_value=SimpleNamespace(subject_id="bob", roles=("viewer",))),
        ):
            with patch("provide.uterm.cloudflare.entry.get_kv_session", new=AsyncMock(return_value=None)):
                resp = await _handle_session_delete(SimpleNamespace(), SimpleNamespace(), "s1", CloudflareConfig())
    # KV is the auth source — missing row must deny (fail closed), not proceed.
    assert resp.status == 404
    mock_del.assert_not_awaited()


# ---------------------------------------------------------------------------
# _handle_session_delete — admin/owner is authorized (entry.py:331->333)
# ---------------------------------------------------------------------------


async def test_handle_session_delete_admin_allowed() -> None:
    """Admin callers can delete any session."""
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.handlers import _handle_session_delete

    session_data = {"owner": "alice", "visibility": "private"}

    with patch("provide.uterm.cloudflare.entry.delete_kv_session", new=AsyncMock()):
        with patch(
            "provide.uterm.cloudflare.entry.auth._decode_jwt_principal",
            new=AsyncMock(return_value=SimpleNamespace(subject_id="bob", roles=("admin",))),
        ):
            with patch(
                "provide.uterm.cloudflare.entry.get_kv_session",
                new=AsyncMock(return_value=session_data),
            ):
                resp = await _handle_session_delete(SimpleNamespace(), SimpleNamespace(), "s1", CloudflareConfig())
    assert resp.status == 200
    assert json.loads(resp.body)["deleted"] is True


# ---------------------------------------------------------------------------
# state/registry.py — get_kv_session exception handler (lines 86-88)
# ---------------------------------------------------------------------------


async def test_get_kv_session_kv_exception_returns_none() -> None:
    """get_kv_session catches KV errors and returns None."""
    from provide.uterm.cloudflare.state.registry import get_kv_session

    kv = AsyncMock()
    kv.get.side_effect = RuntimeError("kv unavailable")
    result = await get_kv_session(SimpleNamespace(SESSION_REGISTRY=kv), "w1")
    assert result is None
