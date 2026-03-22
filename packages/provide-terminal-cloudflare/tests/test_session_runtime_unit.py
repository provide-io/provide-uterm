#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for do/session_runtime.py — all non-CF-runtime branches."""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import jwt
import pytest
from provide_terminal_cloudflare.do.session_runtime import SessionRuntime
from provide_terminal_cloudflare.state.store import LeaseRecord

from provide.terminal.control_stream import ControlChunk, ControlStreamDecoder, DataChunk

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


def _make_env(mode: str = "dev", **extra) -> SimpleNamespace:
    env = SimpleNamespace(AUTH_MODE=mode, **extra)
    if mode == "jwt":
        env.JWT_ALGORITHMS = "HS256"
        env.JWT_PUBLIC_KEY_PEM = _KEY
        if not hasattr(env, "WORKER_BEARER_TOKEN"):
            env.WORKER_BEARER_TOKEN = "test-worker-token"
    return env


def _make_runtime(worker_id: str = "test-worker", mode: str = "dev") -> SessionRuntime:
    ctx = _make_ctx(worker_id)
    env = _make_env(mode)
    return SessionRuntime(ctx, env)


def _decode_sent(raw: str, *, data_frame_type: str | None = None) -> dict:
    decoder = ControlStreamDecoder()
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


def test_constructor_missing_sql_raises() -> None:
    """Line 44: ctx without storage.sql.exec → RuntimeError."""
    ctx = SimpleNamespace(storage=SimpleNamespace(), id=SimpleNamespace(name=lambda: "w"))
    with pytest.raises(RuntimeError, match="sqlite storage"):
        SessionRuntime(ctx, SimpleNamespace(AUTH_MODE="dev"))


def test_constructor_derives_worker_id() -> None:
    """Lines 59-63: _derive_worker_id uses ctx.id.name()."""
    rt = _make_runtime("my-worker")
    assert rt.worker_id == "my-worker"


def test_derive_worker_id_non_callable_name() -> None:
    """Lines 66-70: ctx.id.name is not callable → 'default'."""
    ctx = _make_ctx("any")
    ctx.id = SimpleNamespace(name="not-callable")
    rt = SessionRuntime(ctx, _make_env())
    assert rt.worker_id == "default"


def test_derive_worker_id_name_raises() -> None:
    """Lines 64-70: ctx.id.name() raises → 'default'."""

    def bad_name() -> str:
        raise RuntimeError("failed")

    ctx = _make_ctx("any")
    ctx.id = SimpleNamespace(name=bad_name)
    rt = SessionRuntime(ctx, _make_env())
    assert rt.worker_id == "default"


# ---------------------------------------------------------------------------
# ws_key
# ---------------------------------------------------------------------------


def test_ws_key_generates_unique_keys() -> None:
    """Lines 73-83: two different ws objects get different keys."""
    rt = _make_runtime()
    ws1, ws2 = _MockWs(), _MockWs()
    assert rt.ws_key(ws1) != rt.ws_key(ws2)


def test_ws_key_cached() -> None:
    """Lines 74-77: key is cached on ws object."""
    rt = _make_runtime()
    ws = _MockWs()
    assert rt.ws_key(ws) == rt.ws_key(ws)


# ---------------------------------------------------------------------------
# _restore_state
# ---------------------------------------------------------------------------


def test_restore_state_with_saved_lease() -> None:
    """Lines 89-102: restore a live hijack session from SQLite."""
    rt = _make_runtime("w1")
    rt.store.save_lease(
        LeaseRecord(worker_id="w1", hijack_id="hid-123", owner="alice", lease_expires_at=time.time() + 300)
    )
    rt.hijack._session = None
    rt._restore_state()
    assert rt.hijack.session is not None
    assert rt.hijack.session.owner == "alice"


def test_restore_state_with_snapshot() -> None:
    """Lines 103-105: restore last_snapshot from SQLite."""
    rt = _make_runtime()
    rt.store.save_snapshot(rt.worker_id, {"type": "snapshot", "screen": "hello"})
    rt.last_snapshot = None
    rt._restore_state()
    assert rt.last_snapshot is not None


def test_restore_state_with_input_mode() -> None:
    """Lines 106-108: restore input_mode from SQLite."""
    rt = _make_runtime()
    rt.store.save_input_mode(rt.worker_id, "open")
    rt.input_mode = "hijack"
    rt._restore_state()
    assert rt.input_mode == "open"


# ---------------------------------------------------------------------------
# _extract_token
# ---------------------------------------------------------------------------


def test_extract_token_from_bearer_header() -> None:
    """Lines 118-123: Authorization: Bearer xyz → 'xyz'."""
    rt = _make_runtime()
    req = _MockRequest(headers={"Authorization": "Bearer my-token"})
    assert rt._extract_token(req) == "my-token"


def test_extract_token_query_param_disabled() -> None:
    """Lines 124-125: allow_query_token=False → None even with URL param."""
    rt = _make_runtime()
    rt.config.jwt.allow_query_token = False
    req = _MockRequest(url="https://x/path?token=qtoken")
    assert rt._extract_token(req) is None


def test_extract_token_from_query_param() -> None:
    """Lines 126-130: token in ?token= when allow_query_token=True."""
    rt = _make_runtime()
    rt.config.jwt.allow_query_token = True
    req = _MockRequest(url="https://x/path?token=qtoken")
    assert rt._extract_token(req) == "qtoken"


def test_extract_token_no_header_no_query() -> None:
    """Lines 118-133: no auth header, no query param → None."""
    rt = _make_runtime()
    rt.config.jwt.allow_query_token = True
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
