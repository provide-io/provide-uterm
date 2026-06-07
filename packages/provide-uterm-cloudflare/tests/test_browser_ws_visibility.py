#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for browser WebSocket upgrade visibility enforcement in SessionRuntime."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import jwt
import pytest
from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

_JWT_KEY = "test-secret-key-32-bytes-minimum!"


class _Req:
    def __init__(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None):
        self.url = url
        self.method = method
        self.headers = headers or {}


def _make_runtime_with_token(token: str | None = None, mode: str = "dev") -> SessionRuntime:
    ctx = SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=sqlite3.connect(":memory:").execute),
            setAlarm=lambda ms: None,
        ),
        id=SimpleNamespace(name=lambda: "test-worker"),
        getWebSockets=list,
        acceptWebSocket=lambda ws: None,
    )
    # from_env only accepts jwt mode now; always build a valid jwt config, then
    # override the in-memory mode for tests exercising the legacy open-access branches.
    env_kwargs: dict = {
        "AUTH_MODE": "jwt",
        "JWT_ALGORITHMS": "HS256",
        "JWT_PUBLIC_KEY_PEM": _JWT_KEY,
        "WORKER_BEARER_TOKEN": token if token is not None else "test-worker-token-padded-to-32xyz",
    }
    rt = SessionRuntime(ctx, SimpleNamespace(**env_kwargs))
    rt.config.jwt.mode = mode
    return rt


def _viewer_jwt(sub: str = "viewer-user") -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": sub, "roles": ["viewer"], "iat": now, "nbf": now, "exp": now + 600},
        _JWT_KEY,
        algorithm="HS256",
    )


def _operator_jwt(sub: str = "op-user") -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": sub, "roles": ["operator"], "iat": now, "nbf": now, "exp": now + 600},
        _JWT_KEY,
        algorithm="HS256",
    )


def _admin_jwt(sub: str = "admin-user") -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": sub, "roles": ["admin"], "iat": now, "nbf": now, "exp": now + 600},
        _JWT_KEY,
        algorithm="HS256",
    )


def _fake_js_module() -> ModuleType:
    """Return a mock 'js' module with a WebSocketPair that produces (101) upgrades."""
    fake_js = ModuleType("js")
    pair = MagicMock()
    pair.new.return_value = MagicMock(object_values=MagicMock(return_value=(MagicMock(), MagicMock())))
    fake_js.WebSocketPair = pair  # type: ignore[attr-defined]
    return fake_js


# ---------------------------------------------------------------------------
# Browser WS upgrade: session visibility blocks unauthorized callers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_ws_upgrade_blocked_for_private_session() -> None:
    """Browser WS upgrade to a private session returns 403 for a non-owner viewer."""
    runtime = _make_runtime_with_token(token="test-worker-token-padded-to-32xyz", mode="jwt")
    runtime.meta["visibility"] = "private"
    runtime.meta["owner"] = "alice"

    req = _Req(
        "https://example.invalid/ws/browser/test-worker",
        headers={"Upgrade": "websocket", "Authorization": f"Bearer {_viewer_jwt('bob')}"},
    )

    sys.modules["js"] = _fake_js_module()
    try:
        resp = await runtime.fetch(req)
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "forbidden"
    finally:
        sys.modules.pop("js", None)


@pytest.mark.asyncio
async def test_browser_ws_upgrade_allowed_for_operator_visibility_with_operator_role() -> None:
    """Browser WS upgrade to an operator-visibility session succeeds for an operator."""
    runtime = _make_runtime_with_token(token="test-worker-token-padded-to-32xyz", mode="jwt")
    runtime.meta["visibility"] = "operator"
    runtime.meta["owner"] = "alice"

    req = _Req(
        "https://example.invalid/ws/browser/test-worker",
        headers={"Upgrade": "websocket", "Authorization": f"Bearer {_operator_jwt('bob')}"},
    )

    sys.modules["js"] = _fake_js_module()
    try:
        resp = await runtime.fetch(req)
        assert resp.status == 101
    finally:
        sys.modules.pop("js", None)


@pytest.mark.asyncio
async def test_browser_ws_upgrade_allowed_for_private_session_with_admin_role() -> None:
    """Admin browser WS is allowed through a private session without ownership check."""
    runtime = _make_runtime_with_token(token="test-worker-token-padded-to-32xyz", mode="jwt")
    runtime.meta["visibility"] = "private"
    runtime.meta["owner"] = "alice"

    req = _Req(
        "https://example.invalid/ws/browser/test-worker",
        headers={"Upgrade": "websocket", "Authorization": f"Bearer {_admin_jwt()}"},
    )

    sys.modules["js"] = _fake_js_module()
    try:
        resp = await runtime.fetch(req)
        assert resp.status == 101
    finally:
        sys.modules.pop("js", None)


@pytest.mark.asyncio
async def test_browser_ws_upgrade_blocked_cross_site() -> None:
    """A cross-site browser WS handshake is rejected (CSWSH defense) even with valid
    auth — the cookie-authed browser socket must not be hijackable from another origin."""
    runtime = _make_runtime_with_token(token="test-worker-token-padded-to-32xyz", mode="jwt")
    runtime.meta["visibility"] = "public"

    req = _Req(
        "https://example.invalid/ws/browser/test-worker",
        headers={
            "Upgrade": "websocket",
            "Authorization": f"Bearer {_admin_jwt()}",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    sys.modules["js"] = _fake_js_module()
    try:
        resp = await runtime.fetch(req)
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "cross_site_blocked"
    finally:
        sys.modules.pop("js", None)


@pytest.mark.asyncio
async def test_raw_ws_upgrade_not_cross_site_checked() -> None:
    """Worker/raw sockets are bearer-authed (no ambient cookie) and send no Origin,
    so the cross-site guard does not apply — a cross-site header must not block them."""
    runtime = _make_runtime_with_token(token="test-worker-token-padded-to-32xyz", mode="jwt")

    req = _Req(
        "https://example.invalid/ws/raw/test-worker",
        headers={
            "Upgrade": "websocket",
            "Authorization": "Bearer test-worker-token-padded-to-32xyz",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    sys.modules["js"] = _fake_js_module()
    try:
        resp = await runtime.fetch(req)
        assert resp.status == 101  # raw socket bypasses the browser-only cross-site check
    finally:
        sys.modules.pop("js", None)


@pytest.mark.asyncio
async def test_browser_ws_upgrade_blocked_for_operator_session_with_viewer_role() -> None:
    """Browser WS upgrade to an operator-visibility session returns 403 for a viewer."""
    runtime = _make_runtime_with_token(token="test-worker-token-padded-to-32xyz", mode="jwt")
    runtime.meta["visibility"] = "operator"
    runtime.meta["owner"] = "alice"

    req = _Req(
        "https://example.invalid/ws/browser/test-worker",
        headers={"Upgrade": "websocket", "Authorization": f"Bearer {_viewer_jwt('bob')}"},
    )

    sys.modules["js"] = _fake_js_module()
    try:
        resp = await runtime.fetch(req)
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "forbidden"
    finally:
        sys.modules.pop("js", None)


# ---------------------------------------------------------------------------
# Raw WebSocket auth regression — /ws/raw/ must use bearer token in JWT mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_ws_upgrade_in_jwt_mode_accepts_bearer_token() -> None:
    """/ws/raw/ with a valid worker bearer token upgrades to 101 in JWT mode.

    Regression: previously /ws/raw/ fell through to JWT resolution, causing
    401 'Not enough segments' when the bearer token was not a valid JWT.
    """
    runtime = _make_runtime_with_token(token="test-worker-token-padded-to-32xyz", mode="jwt")

    req = _Req(
        "https://example.invalid/ws/raw/test-worker/term",
        headers={"Upgrade": "websocket", "Authorization": "Bearer test-worker-token-padded-to-32xyz"},
    )

    sys.modules["js"] = _fake_js_module()
    try:
        resp = await runtime.fetch(req)
        assert resp.status == 101
    finally:
        sys.modules.pop("js", None)


@pytest.mark.asyncio
async def test_raw_ws_upgrade_in_jwt_mode_rejects_wrong_token() -> None:
    """/ws/raw/ with a wrong bearer token returns 403 in JWT mode."""
    runtime = _make_runtime_with_token(token="test-worker-token-padded-to-32xyz", mode="jwt")

    req = _Req(
        "https://example.invalid/ws/raw/test-worker/term",
        headers={"Upgrade": "websocket", "Authorization": "Bearer wrong-token"},
    )

    sys.modules["js"] = _fake_js_module()
    try:
        resp = await runtime.fetch(req)
        assert resp.status == 403
    finally:
        sys.modules.pop("js", None)
