#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for session_runtime.py — _resolve_principal and worker WS auth."""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import patch

import jwt

_KEY = "test-secret-key-32-bytes-minimum!"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_ctx(worker_id: str = "test-worker"):
    conn = sqlite3.connect(":memory:")
    return SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            setAlarm=lambda ms: None,
        ),
        id=SimpleNamespace(name=lambda: worker_id),
        getWebSockets=list,
    )


def _make_env(mode: str = "dev", **extra) -> SimpleNamespace:
    env = SimpleNamespace(AUTH_MODE=mode, **extra)
    if mode == "jwt":
        env.JWT_ALGORITHMS = "HS256"
        env.JWT_PUBLIC_KEY_PEM = _KEY
        if not hasattr(env, "WORKER_BEARER_TOKEN"):
            env.WORKER_BEARER_TOKEN = "test-worker-token"
    return env


def _make_runtime(worker_id: str = "test-worker", mode: str = "dev"):
    from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

    ctx = _make_ctx(worker_id)
    env = _make_env(mode)
    return SessionRuntime(ctx, env)


def _make_token(sub: str = "user") -> str:
    now = int(time.time())
    return jwt.encode({"sub": sub, "exp": now + 600}, _KEY, algorithm="HS256")


# ---------------------------------------------------------------------------
# _resolve_principal with CF-Access-Client-Id (lines 136-140)
# ---------------------------------------------------------------------------


async def test_resolve_principal_cf_access_client_id_without_suffix_rejected() -> None:
    """Finding #1: a CF-Access-Client-Id without the ``.access`` suffix must NOT bypass JWT.

    Before the fix this path used ``len(cf_client_id) > 0`` so any non-empty
    string passed by the caller (no real CF Access validation upstream) was
    accepted as service-token auth.  Sibling files use ``.endswith(".access")``;
    this path is brought into line so a plain ``CF-Access-Client-Id: x`` request
    falls through to normal JWT validation (and fails when no token is present).
    """
    rt = _make_runtime(mode="jwt")

    def _get(k, default=None):
        if k == "CF-Access-Client-Id":
            return "some-client-id"
        return None

    req = SimpleNamespace(url="https://x/ws/worker/test/term", headers=SimpleNamespace(get=_get))
    principal, error = await rt._resolve_principal(req)
    assert principal is None
    # No JWT token → falls through to 401, NOT silently authorised.
    assert error is not None
    assert error.status == 401


async def test_resolve_principal_cf_access_client_id_with_access_suffix() -> None:
    """CF-Access-Client-Id ending in .access does not bypass JWT in DO."""
    rt = _make_runtime(mode="jwt")

    def _get(k, default=None):
        if k == "CF-Access-Client-Id":
            return "my-client.access"
        return None

    req = SimpleNamespace(url="https://x/ws/worker/test/term", headers=SimpleNamespace(get=_get))
    principal, error = await rt._resolve_principal(req)
    assert principal is None
    assert error is not None
    assert error.status == 401


async def test_resolve_principal_cf_access_header_exception_falls_through() -> None:
    """If headers.get raises for CF-Access-Client-Id, falls through to token check."""
    rt = _make_runtime(mode="jwt")

    def _get(k, default=None):
        if k == "CF-Access-Client-Id":
            raise RuntimeError("headers broken")
        if k == "Authorization":
            return f"Bearer {_make_token()}"
        return None

    req = SimpleNamespace(url="https://x/test", headers=SimpleNamespace(get=_get))
    principal, error = await rt._resolve_principal(req)
    assert error is None


# ---------------------------------------------------------------------------
# Worker WS with CF-Access-Client-Id (line 219-220)
# ---------------------------------------------------------------------------


async def test_worker_ws_cf_access_header_does_not_bypass_bearer_token() -> None:
    """Worker WS with CF-Access-Client-Id .access suffix must still require a bearer token."""
    rt = _make_runtime(mode="jwt")

    def _get(k, default=None):
        if k == "Upgrade":
            return "websocket"
        if k in ("CF-Access-Client-Id", "cf-access-client-id"):
            return "my-service.access"
        return None

    req = SimpleNamespace(
        url="https://x/ws/worker/test-worker/term",
        method="GET",
        headers=SimpleNamespace(get=_get),
    )

    # With no bearer token, the request must be rejected before any websocket
    # machinery (WebSocketPair) is needed.
    with patch("provide.uterm.cloudflare.do.session_runtime.fetch.extract_bearer_or_cookie", return_value=None):
        resp = await rt.fetch(req)
        assert resp.status == 403
