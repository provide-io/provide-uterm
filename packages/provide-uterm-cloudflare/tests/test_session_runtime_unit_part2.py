#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for do/session_runtime.py — all non-CF-runtime branches."""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import jwt
from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

from provide.uterm.control_channel import ControlChannelDecoder, ControlChunk, DataChunk

_KEY = "test-secret-key-32-bytes-minimum!"


def _make_token(sub: str = "user", roles: list[str] | None = None) -> str:
    now = int(time.time())
    payload: dict = {"sub": sub, "iat": now, "exp": now + 600}
    if roles:
        payload["roles"] = roles
    return jwt.encode(payload, _KEY, algorithm="HS256")


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


def _make_env(mode: str = "jwt", **extra) -> SimpleNamespace:
    # from_env only accepts jwt mode now; always emit a valid jwt config.
    env = SimpleNamespace(AUTH_MODE="jwt", **extra)
    env.JWT_ALGORITHMS = "HS256"
    env.JWT_PUBLIC_KEY_PEM = _KEY
    if not hasattr(env, "WORKER_BEARER_TOKEN"):
        env.WORKER_BEARER_TOKEN = "test-worker-token-padded-to-32xyz"
    return env


def _make_runtime(worker_id: str = "test-worker", mode: str = "dev") -> SessionRuntime:
    # from_env only accepts jwt mode now; build a valid jwt config, then override
    # the in-memory mode for tests that exercise the legacy open-access branches.
    ctx = _make_ctx(worker_id)
    rt = SessionRuntime(ctx, _make_env("jwt"))
    rt.config.jwt.mode = mode
    return rt


def _decode_sent(raw: str, *, data_frame_type: str | None = None) -> dict:
    decoder = ControlChannelDecoder()
    events = decoder.feed(raw)
    events.extend(decoder.finish())
    assert len(events) == 1
    event = events[0]
    if isinstance(event, ControlChunk):
        return event.control
    if isinstance(event, DataChunk):
        return {"type": data_frame_type or "term", "data": event.data}
    raise AssertionError("unexpected decoder event")


class _MockWs:
    """Sync-send WebSocket stub."""

    def __init__(self, attachment: object = None) -> None:
        self._attachment = attachment
        self.sent: list[str] = []

    def deserializeAttachment(self) -> object:  # noqa: N802
        return self._attachment

    def send(self, data: str) -> None:
        self.sent.append(data)


class _AsyncWs(_MockWs):
    """Async-send WebSocket stub."""

    async def send(self, data: str) -> None:  # type: ignore[override]
        self.sent.append(data)


class _MockRequest:
    """Minimal HTTP request stub."""

    def __init__(
        self,
        url: str = "https://x/worker/test-worker/api/health",
        method: str = "GET",
        headers: dict | None = None,
        body: str = "{}",
    ) -> None:
        self.url = url
        self.method = method
        self._headers = headers or {}
        self._body = body
        self.headers = SimpleNamespace(get=lambda k, d=None: self._headers.get(k, d))

    async def text(self) -> str:
        return self._body


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_extract_token_no_header_no_query() -> None:
    """Lines 118-133: no auth header, no query param → None."""
    rt = _make_runtime()
    req = _MockRequest(url="https://x/path")
    assert rt._extract_token(req) is None


# ---------------------------------------------------------------------------
# _resolve_principal
# ---------------------------------------------------------------------------


async def test_resolve_principal_dev_mode() -> None:
    """Lines 141-142: dev mode → (None, None)."""
    rt = _make_runtime(mode="dev")
    principal, error = await rt._resolve_principal(_MockRequest())
    assert principal is None and error is None


async def test_resolve_principal_jwt_no_token() -> None:
    """Lines 143-149: jwt mode, no token → (None, 401)."""
    rt = _make_runtime(mode="jwt")
    principal, error = await rt._resolve_principal(_MockRequest())
    assert principal is None
    assert error is not None and error.status == 401


async def test_resolve_principal_jwt_valid_token() -> None:
    """Lines 150-152: jwt mode, valid token → (principal, None)."""
    rt = _make_runtime(mode="jwt")
    token = _make_token("alice", ["admin"])
    req = _MockRequest(headers={"Authorization": f"Bearer {token}"})
    principal, error = await rt._resolve_principal(req)
    assert principal is not None and error is None


async def test_resolve_principal_jwt_bad_token() -> None:
    """Lines 153-158: jwt mode, invalid token → (None, 401)."""
    rt = _make_runtime(mode="jwt")
    req = _MockRequest(headers={"Authorization": "Bearer not-a-valid-jwt"})
    principal, error = await rt._resolve_principal(req)
    assert principal is None
    assert error is not None and error.status == 401


# ---------------------------------------------------------------------------
# browser_role_for_request
# ---------------------------------------------------------------------------


async def test_browser_role_dev_mode() -> None:
    """Lines 168-169: dev mode → 'admin'."""
    rt = _make_runtime(mode="dev")
    assert await rt.browser_role_for_request(_MockRequest()) == "admin"


async def test_browser_role_jwt_no_token() -> None:
    """Lines 170-172: jwt mode, no token → 'viewer'."""
    rt = _make_runtime(mode="jwt")
    assert await rt.browser_role_for_request(_MockRequest()) == "viewer"


async def test_browser_role_jwt_valid_token() -> None:
    """Lines 173-176: jwt mode, valid admin token → 'admin'."""
    rt = _make_runtime(mode="jwt")
    token = _make_token("u", ["admin"])
    req = _MockRequest(headers={"Authorization": f"Bearer {token}"})
    assert await rt.browser_role_for_request(req) == "admin"


async def test_browser_role_jwt_bad_token() -> None:
    """Line 177: jwt mode, bad token → 'viewer'."""
    rt = _make_runtime(mode="jwt")
    req = _MockRequest(headers={"Authorization": "Bearer bad"})
    assert await rt.browser_role_for_request(req) == "viewer"


async def test_browser_role_owner_with_viewer_jwt_gets_operator() -> None:
    """Owner of a session must be elevated to operator on mutations even if
    their JWT role is only viewer.  Without this, the visibility layer would
    let the owner READ their session but every POST (mode/hijack/…) would 403.
    Mirrors the hosted FastAPI resolve_browser_role owner-elevation branch.
    """
    rt = _make_runtime(mode="jwt")
    rt.meta["owner"] = "alice"
    token = _make_token("alice", ["viewer"])
    req = _MockRequest(headers={"Authorization": f"Bearer {token}"})
    assert await rt.browser_role_for_request(req) == "operator"


async def test_browser_role_non_owner_viewer_stays_viewer() -> None:
    """A viewer who does NOT own the session stays viewer."""
    rt = _make_runtime(mode="jwt")
    rt.meta["owner"] = "alice"
    token = _make_token("bob", ["viewer"])
    req = _MockRequest(headers={"Authorization": f"Bearer {token}"})
    assert await rt.browser_role_for_request(req) == "viewer"


async def test_browser_role_owner_with_admin_jwt_stays_admin() -> None:
    """An admin who is also owner keeps admin (elevation is a floor, not a cap)."""
    rt = _make_runtime(mode="jwt")
    rt.meta["owner"] = "alice"
    token = _make_token("alice", ["admin"])
    req = _MockRequest(headers={"Authorization": f"Bearer {token}"})
    assert await rt.browser_role_for_request(req) == "admin"


async def test_browser_role_share_token_viewer() -> None:
    from provide.uterm.tunnel.token_hash import hash_token

    rt = _make_runtime(mode="jwt")
    rt._share_token_hash = hash_token("share-token-123")
    req = _MockRequest(
        url="https://x/app/session/test-worker",
        headers={"cookie": "uterm_tunnel_test-worker=share-token-123"},
    )
    assert await rt.browser_role_for_request(req) == "viewer"


async def test_browser_role_share_token_control_maps_to_admin() -> None:
    from provide.uterm.tunnel.token_hash import hash_token

    rt = _make_runtime(mode="jwt")
    rt._control_token_hash = hash_token("control-token-123")
    req = _MockRequest(
        url="https://x/app/operator/test-worker",
        headers={"cookie": "uterm_tunnel_test-worker=control-token-123"},
    )
    assert await rt.browser_role_for_request(req) == "admin"


async def test_resolve_principal_share_token_bypasses_jwt() -> None:
    from provide.uterm.tunnel.token_hash import hash_token

    rt = _make_runtime(mode="jwt")
    rt._share_token_hash = hash_token("share-token-123")
    req = _MockRequest(
        url="https://x/app/session/test-worker",
        headers={"cookie": "uterm_tunnel_test-worker=share-token-123"},
    )
    principal, error = await rt._resolve_principal(req)
    assert principal is None
    assert error is None


# ---------------------------------------------------------------------------
# _socket_role
# ---------------------------------------------------------------------------


def test_socket_role_plain_string_worker() -> None:
    """Lines 188-189: attachment='worker' → 'worker'."""
    rt = _make_runtime()
    assert rt._socket_role(_MockWs(attachment="worker")) == "worker"


def test_socket_role_colon_format_browser() -> None:
    """Lines 191-193: 'browser:admin:w1' → 'browser'."""
    rt = _make_runtime()
    assert rt._socket_role(_MockWs(attachment="browser:admin:w1")) == "browser"


def test_socket_role_colon_format_raw() -> None:
    """Lines 191-193: 'raw:admin:w1' → 'raw'."""
    rt = _make_runtime()
    assert rt._socket_role(_MockWs(attachment="raw:admin:w1")) == "raw"


def test_socket_role_from_instance_attr() -> None:
    """Lines 213-215: no attachment, _ut_role set → returns _ut_role."""
    rt = _make_runtime()
    ws = _MockWs(attachment=None)
    ws._ut_role = "raw"  # type: ignore[attr-defined]
    assert rt._socket_role(ws) == "raw"


def test_socket_role_default_browser() -> None:
    """Line 216: no attachment, no _ut_role → 'browser'."""
    rt = _make_runtime()
    assert rt._socket_role(_MockWs(attachment=None)) == "browser"


def test_socket_role_deserialize_raises() -> None:
    """Lines 210-211: deserializeAttachment raises → 'browser'."""
    rt = _make_runtime()

    def bad_deser() -> None:
        raise RuntimeError("err")

    ws = SimpleNamespace(deserializeAttachment=bad_deser)
    assert rt._socket_role(ws) == "browser"


# ---------------------------------------------------------------------------
# _socket_browser_role
# ---------------------------------------------------------------------------


def test_socket_browser_role_from_colon_attachment() -> None:
    """Lines 228-231: 'browser:operator' → 'operator'."""
    rt = _make_runtime()
    assert rt._socket_browser_role(_MockWs(attachment="browser:operator")) == "operator"


def test_socket_browser_role_from_instance_attr() -> None:
    """Lines 237-239: _ut_browser_role='admin' → 'admin'."""
    rt = _make_runtime()
    ws = _MockWs(attachment=None)
    ws._ut_browser_role = "admin"  # type: ignore[attr-defined]
    assert rt._socket_browser_role(ws) == "admin"


def test_socket_browser_role_dev_mode_default() -> None:
    """Lines 240-241: dev mode, no attachment → 'admin'."""
    rt = _make_runtime(mode="dev")
    assert rt._socket_browser_role(_MockWs(attachment=None)) == "admin"


def test_socket_browser_role_jwt_mode_default() -> None:
    """Line 241: jwt mode, no attachment → 'viewer'."""
    rt = _make_runtime(mode="jwt")
    assert rt._socket_browser_role(_MockWs(attachment=None)) == "viewer"


# ---------------------------------------------------------------------------
# _socket_worker_id
# ---------------------------------------------------------------------------


def test_socket_worker_id_from_attachment() -> None:
    """Lines 253-254: 'browser:admin:my-worker' → 'my-worker'."""
    rt = _make_runtime()
    assert rt._socket_worker_id(_MockWs(attachment="browser:admin:my-worker")) == "my-worker"


def test_socket_worker_id_fallback() -> None:
    """Line 257: no attachment → runtime.worker_id."""
    rt = _make_runtime("fallback-worker")
    assert rt._socket_worker_id(_MockWs(attachment=None)) == "fallback-worker"


# ---------------------------------------------------------------------------
# _register_socket
# ---------------------------------------------------------------------------


def test_register_worker_socket() -> None:
    """Lines 261-263: role='worker' → sets worker_ws."""
    rt = _make_runtime()
    ws = _MockWs()
    rt._register_socket(ws, "worker")
    assert rt.worker_ws is ws


def test_register_raw_socket() -> None:
    """Lines 264-266: role='raw' → added to raw_sockets."""
    rt = _make_runtime()
    ws = _MockWs()
    rt._register_socket(ws, "raw")
    assert ws in rt.raw_sockets.values()


def test_register_browser_socket() -> None:
    """Line 267: role='browser' → added to browser_sockets."""
    rt = _make_runtime()
    ws = _MockWs()
    rt._register_socket(ws, "browser")
    assert ws in rt.browser_sockets.values()
