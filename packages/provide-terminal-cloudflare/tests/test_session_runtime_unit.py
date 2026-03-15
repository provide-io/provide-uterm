#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for do/session_runtime.py — all non-CF-runtime branches."""

from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from provide_terminal_cloudflare.bridge.hijack import HijackSession
from provide_terminal_cloudflare.do.session_runtime import SessionRuntime
from provide_terminal_cloudflare.state.store import LeaseRecord

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


# ---------------------------------------------------------------------------
# _lazy_init_worker_id
# ---------------------------------------------------------------------------


def test_lazy_init_from_ws_worker_url() -> None:
    """Lines 285-290: worker_id='default' + URL → updates worker_id."""
    ctx = _make_ctx("any")
    ctx.id = SimpleNamespace(name=lambda: "default")
    rt = SessionRuntime(ctx, _make_env())
    assert rt.worker_id == "default"
    rt._lazy_init_worker_id(_MockRequest(url="https://x/ws/worker/real-id/term"))
    assert rt.worker_id == "real-id"


def test_lazy_init_from_worker_http_url() -> None:
    """Lines 285-290: /worker/ prefix also updates."""
    ctx = _make_ctx("any")
    ctx.id = SimpleNamespace(name=lambda: "default")
    rt = SessionRuntime(ctx, _make_env())
    rt._lazy_init_worker_id(_MockRequest(url="https://x/worker/hijack-id/hijack/acquire"))
    assert rt.worker_id == "hijack-id"


def test_lazy_init_skips_if_not_default() -> None:
    """Lines 279-280: worker_id != 'default' → no change."""
    rt = _make_runtime("existing-id")
    rt._lazy_init_worker_id(_MockRequest(url="https://x/ws/worker/other-id/term"))
    assert rt.worker_id == "existing-id"


# ---------------------------------------------------------------------------
# fetch() — non-WebSocket path
# ---------------------------------------------------------------------------


async def test_fetch_returns_401_in_jwt_mode_no_token() -> None:
    """Lines 296-298: jwt mode, no token → 401."""
    rt = _make_runtime(mode="jwt")
    resp = await rt.fetch(_MockRequest(url="https://x/worker/w/api/health"))
    assert resp.status == 401


async def test_fetch_routes_to_http_handler() -> None:
    """Line 369: dev mode, no upgrade header → route_http → 200."""
    rt = _make_runtime(mode="dev")
    # route_http matches path exactly; /api/health is the only zero-auth path
    resp = await rt.fetch(_MockRequest(url="https://x/api/health"))
    assert resp.status == 200


# ---------------------------------------------------------------------------
# webSocketOpen
# ---------------------------------------------------------------------------


async def test_websocket_open_browser_sends_hello() -> None:
    """Lines 392-411: browser socket → hello frame sent."""
    rt = _make_runtime()
    ws = _MockWs(attachment="browser:admin:test-worker")
    await rt.webSocketOpen(ws)
    types = [json.loads(m).get("type") for m in ws.sent]
    assert "hello" in types


async def test_websocket_open_worker_sets_worker_ws() -> None:
    """Lines 375-386: worker socket → worker_ws set."""
    rt = _make_runtime()
    ws = _MockWs(attachment="worker:admin:test-worker")
    await rt.webSocketOpen(ws)
    assert rt.worker_ws is ws


async def test_websocket_open_raw_with_snapshot_sends_screen() -> None:
    """Lines 387-390: raw socket + snapshot → sends screen text."""
    rt = _make_runtime()
    rt.last_snapshot = {"type": "snapshot", "screen": "hello world"}
    ws = _MockWs(attachment="raw:admin:test-worker")
    await rt.webSocketOpen(ws)
    assert any("hello world" in m for m in ws.sent)


# ---------------------------------------------------------------------------
# webSocketMessage
# ---------------------------------------------------------------------------


async def test_websocket_message_raw_text() -> None:
    """Lines 416-420: raw socket text → push_worker_input."""
    rt = _make_runtime()
    ws = _MockWs(attachment="raw:admin:test-worker")
    rt._register_socket(ws, "raw")
    worker_ws = _MockWs()
    rt.worker_ws = worker_ws
    await rt.webSocketMessage(ws, "ls\r\n")
    assert any("ls" in json.loads(m).get("data", "") for m in worker_ws.sent)


async def test_websocket_message_raw_bytes() -> None:
    """Lines 417-418: raw bytes → decoded as latin-1."""
    rt = _make_runtime()
    ws = _MockWs(attachment="raw:admin:test-worker")
    rt._register_socket(ws, "raw")
    worker_ws = _MockWs()
    rt.worker_ws = worker_ws
    await rt.webSocketMessage(ws, b"cmd\r")
    assert any("cmd" in json.loads(m).get("data", "") for m in worker_ws.sent)


async def test_websocket_message_worker_calls_handle_socket_message() -> None:
    """Lines 423-424: worker socket → handle_socket_message called."""
    rt = _make_runtime()
    ws = _MockWs(attachment="worker:admin:test-worker")
    with patch(
        "provide_terminal_cloudflare.do.session_runtime.handle_socket_message",
        new=AsyncMock(return_value=None),
    ) as mock_handle:
        await rt.webSocketMessage(ws, '{"type":"snapshot"}')
        mock_handle.assert_called_once()


# ---------------------------------------------------------------------------
# _remove_ws / webSocketClose / webSocketError
# ---------------------------------------------------------------------------


def test_remove_ws_worker() -> None:
    """Lines 429-430: ws is worker_ws → worker_ws set to None."""
    rt = _make_runtime()
    ws = _MockWs()
    rt.worker_ws = ws
    rt._remove_ws(ws)
    assert rt.worker_ws is None


def test_remove_ws_browser() -> None:
    """Line 431: browser ws removed from browser_sockets."""
    rt = _make_runtime()
    ws = _MockWs()
    ws_id = rt.ws_key(ws)
    rt.browser_sockets[ws_id] = ws
    rt._remove_ws(ws)
    assert ws_id not in rt.browser_sockets


async def test_websocket_close_worker_broadcasts_disconnected() -> None:
    """Lines 436-444: worker closes → worker_disconnected broadcast."""
    rt = _make_runtime()
    ws = _MockWs(attachment="worker:admin:test-worker")
    rt.worker_ws = ws
    browser = _MockWs(attachment="browser:admin:test-worker")
    rt._register_socket(browser, "browser")
    # Make getWebSockets raise so broadcast_to_browsers falls back to browser_sockets
    rt.ctx.getWebSockets = lambda: (_ for _ in ()).throw(RuntimeError("no ws"))

    def _raise() -> list:
        raise RuntimeError("no ws")

    rt.ctx.getWebSockets = _raise
    await rt.webSocketClose(ws, 1000, "normal", True)
    assert rt.worker_ws is None
    types = [json.loads(m).get("type") for m in browser.sent]
    assert "worker_disconnected" in types


async def test_websocket_close_browser_just_removes() -> None:
    """Lines 436-441: browser closes → removed, no broadcast."""
    rt = _make_runtime()
    ws = _MockWs(attachment="browser:admin:test-worker")
    rt._register_socket(ws, "browser")
    await rt.webSocketClose(ws, 1000, "normal")
    assert ws not in rt.browser_sockets.values()


async def test_websocket_error_worker_broadcasts_disconnected() -> None:
    """Lines 447-453: worker error → worker_disconnected broadcast."""
    rt = _make_runtime()
    ws = _MockWs(attachment="worker:admin:test-worker")
    rt.worker_ws = ws
    browser = _MockWs(attachment="browser:admin:test-worker")
    rt._register_socket(browser, "browser")

    def _raise() -> list:
        raise RuntimeError("no ws")

    rt.ctx.getWebSockets = _raise
    await rt.webSocketError(ws, "timeout")
    types = [json.loads(m).get("type") for m in browser.sent]
    assert "worker_disconnected" in types


# ---------------------------------------------------------------------------
# request_json
# ---------------------------------------------------------------------------


async def test_request_json_valid() -> None:
    """Lines 460-466: valid JSON body → dict."""
    rt = _make_runtime()
    assert await rt.request_json(_MockRequest(body='{"key": "value"}')) == {"key": "value"}


async def test_request_json_empty_body() -> None:
    """Lines 461-462: empty body → {}."""
    rt = _make_runtime()
    assert await rt.request_json(_MockRequest(body="")) == {}


async def test_request_json_non_dict() -> None:
    """Lines 464-465: list JSON → {}."""
    rt = _make_runtime()
    assert await rt.request_json(_MockRequest(body="[1, 2]")) == {}


# ---------------------------------------------------------------------------
# persist_lease / clear_lease
# ---------------------------------------------------------------------------


def test_persist_lease_none_is_noop() -> None:
    """Line 470: persist_lease(None) → returns early."""
    rt = _make_runtime()
    rt.persist_lease(None)  # should not raise


def test_persist_lease_saves_to_store() -> None:
    """Lines 471-480: saves lease to SQLite store."""
    rt = _make_runtime()
    session = HijackSession(hijack_id="h1", owner="alice", lease_expires_at=time.time() + 300)
    rt.persist_lease(session)
    row = rt.store.load_session(rt.worker_id)
    assert row is not None and row["hijack_id"] == "h1"


def test_clear_lease_removes_from_store() -> None:
    """Line 483: clear_lease clears hijack from store."""
    rt = _make_runtime()
    rt.store.save_lease(
        LeaseRecord(worker_id=rt.worker_id, hijack_id="h1", owner="a", lease_expires_at=time.time() + 300)
    )
    rt.clear_lease()
    row = rt.store.load_session(rt.worker_id)
    assert row is None or row.get("hijack_id") is None


# ---------------------------------------------------------------------------
# send helpers
# ---------------------------------------------------------------------------


async def test_send_ws_serializes_and_sends() -> None:
    """Lines 489-490: send_ws serializes dict and calls ws.send."""
    rt = _make_runtime()
    ws = _MockWs()
    await rt.send_ws(ws, {"type": "test"})
    assert ws.sent and json.loads(ws.sent[0])["type"] == "test"


async def test_send_text_async_ws_is_awaited() -> None:
    """Lines 493-495: async ws.send is awaited."""
    rt = _make_runtime()
    ws = _AsyncWs()
    await rt._send_text(ws, "hello")
    assert ws.sent == ["hello"]


async def test_send_hijack_state_no_session() -> None:
    """Lines 498-512: no active session → hijacked=False."""
    rt = _make_runtime()
    ws = _MockWs()
    await rt.send_hijack_state(ws)
    data = json.loads(ws.sent[0])
    assert data["type"] == "hijack_state" and data["hijacked"] is False


async def test_send_hijack_state_with_session_me() -> None:
    """Lines 498-512: active session, matching hijack_id → owner='me'."""
    rt = _make_runtime()
    session = HijackSession(hijack_id="h1", owner="alice", lease_expires_at=time.time() + 300)
    rt.hijack._session = session
    ws = _MockWs()
    rt.browser_hijack_owner[rt.ws_key(ws)] = "h1"
    await rt.send_hijack_state(ws)
    data = json.loads(ws.sent[0])
    assert data["hijacked"] is True and data["owner"] == "me"


async def test_send_hijack_state_with_session_other() -> None:
    """Lines 498-512: active session, different hijack_id → owner='other'."""
    rt = _make_runtime()
    rt.hijack._session = HijackSession(hijack_id="h1", owner="alice", lease_expires_at=time.time() + 300)
    ws = _MockWs()
    await rt.send_hijack_state(ws)
    data = json.loads(ws.sent[0])
    assert data["owner"] == "other"


async def test_broadcast_hijack_state_sends_to_all_browsers() -> None:
    """Lines 515-520: sends to all browser sockets."""
    rt = _make_runtime()
    ws1, ws2 = _MockWs(), _MockWs()
    rt._register_socket(ws1, "browser")
    rt._register_socket(ws2, "browser")
    await rt.broadcast_hijack_state()
    assert ws1.sent and ws2.sent


# ---------------------------------------------------------------------------
# push_worker_control / push_worker_input
# ---------------------------------------------------------------------------


async def test_push_worker_control_no_ws_returns_false() -> None:
    """Lines 527-528: no worker_ws → False."""
    rt = _make_runtime()
    assert await rt.push_worker_control("pause", owner="a", lease_s=60) is False


async def test_push_worker_control_sends_frame() -> None:
    """Lines 529-533: worker_ws present → control frame sent, returns True."""
    rt = _make_runtime()
    ws = _MockWs()
    rt.worker_ws = ws
    assert await rt.push_worker_control("pause", owner="a", lease_s=60) is True
    assert json.loads(ws.sent[0])["type"] == "control"


async def test_push_worker_input_no_ws_returns_false() -> None:
    """Lines 536-537: no worker_ws → False."""
    rt = _make_runtime()
    assert await rt.push_worker_input("ls\r") is False


async def test_push_worker_input_sends_frame() -> None:
    """Lines 538-539: worker_ws present → input frame sent, returns True."""
    rt = _make_runtime()
    ws = _MockWs()
    rt.worker_ws = ws
    assert await rt.push_worker_input("ls\r") is True
    assert json.loads(ws.sent[0])["type"] == "input"


# ---------------------------------------------------------------------------
# broadcast_to_browsers / broadcast_worker_frame
# ---------------------------------------------------------------------------


async def test_broadcast_to_browsers_via_ctx() -> None:
    """Lines 544-556: uses ctx.getWebSockets() to enumerate sockets."""
    rt = _make_runtime()
    ws = _MockWs(attachment="browser:admin:test-worker")
    rt.ctx.getWebSockets = lambda: [ws]
    await rt.broadcast_to_browsers({"type": "test"})
    assert ws.sent


async def test_broadcast_to_browsers_skips_worker_socket() -> None:
    """Lines 549-550: non-browser sockets are skipped."""
    rt = _make_runtime()
    worker_ws = _MockWs(attachment="worker:admin:test-worker")
    rt.ctx.getWebSockets = lambda: [worker_ws]
    await rt.broadcast_to_browsers({"type": "test"})
    assert not worker_ws.sent


async def test_broadcast_to_browsers_fallback_on_ctx_error() -> None:
    """Lines 546-547: ctx.getWebSockets() raises → falls back to browser_sockets."""
    rt = _make_runtime()
    ws = _MockWs(attachment="browser:admin:test-worker")
    rt._register_socket(ws, "browser")

    def bad_get() -> None:
        raise RuntimeError("no")

    rt.ctx.getWebSockets = bad_get
    await rt.broadcast_to_browsers({"type": "test"})
    assert ws.sent


async def test_broadcast_worker_frame_term_to_raw() -> None:
    """Lines 564-565: term frame → raw sockets get data text."""
    rt = _make_runtime()
    raw_ws = _MockWs()
    rt.raw_sockets[rt.ws_key(raw_ws)] = raw_ws
    await rt.broadcast_worker_frame({"type": "term", "data": "output"})
    assert any("output" in m for m in raw_ws.sent)


async def test_broadcast_worker_frame_snapshot_to_raw() -> None:
    """Lines 566-568: snapshot frame → raw sockets get screen text."""
    rt = _make_runtime()
    raw_ws = _MockWs()
    rt.raw_sockets[rt.ws_key(raw_ws)] = raw_ws
    await rt.broadcast_worker_frame({"type": "snapshot", "screen": "my-screen"})
    assert any("my-screen" in m for m in raw_ws.sent)


async def test_broadcast_worker_frame_worker_connected() -> None:
    """Lines 569-570: worker_connected → raw sockets get text."""
    rt = _make_runtime()
    raw_ws = _MockWs()
    rt.raw_sockets[rt.ws_key(raw_ws)] = raw_ws
    await rt.broadcast_worker_frame({"type": "worker_connected"})
    assert any("[worker connected]" in m for m in raw_ws.sent)


async def test_broadcast_worker_frame_worker_disconnected() -> None:
    """Lines 571-572: worker_disconnected → raw sockets get text."""
    rt = _make_runtime()
    raw_ws = _MockWs()
    rt.raw_sockets[rt.ws_key(raw_ws)] = raw_ws
    await rt.broadcast_worker_frame({"type": "worker_disconnected"})
    assert any("[worker disconnected]" in m for m in raw_ws.sent)


async def test_broadcast_worker_frame_no_text_for_unknown_type() -> None:
    """Lines 574-575: unknown frame type → raw sockets get nothing."""
    rt = _make_runtime()
    raw_ws = _MockWs()
    rt.raw_sockets[rt.ws_key(raw_ws)] = raw_ws
    await rt.broadcast_worker_frame({"type": "other"})
    assert not raw_ws.sent


# ---------------------------------------------------------------------------
# alarm
# ---------------------------------------------------------------------------


async def test_alarm_releases_expired_lease() -> None:
    """Lines 587-592: expired lease → released, resume control sent to worker."""
    rt = _make_runtime()
    ws = _MockWs()
    rt.worker_ws = ws
    # Lease is expired (< now), but _active_session would auto-clear it before alarm() sees it.
    # Patch _active_session to return the session so alarm()'s lease_expires_at <= now check fires.
    session = HijackSession(hijack_id="h1", owner="alice", lease_expires_at=time.time() - 1)
    rt.hijack._session = session
    with patch.object(rt.hijack, "_active_session", return_value=session):
        await rt.alarm()
    assert rt.hijack._session is None  # released
    # worker should receive a "resume" control frame
    control = [json.loads(m) for m in ws.sent if json.loads(m).get("type") == "control"]
    assert any(f.get("action") == "resume" for f in control)


async def test_alarm_kv_refresh_when_worker_connected() -> None:
    """Lines 593-602: worker_ws present → alarm rescheduled."""
    rt = _make_runtime()
    rt.worker_ws = _MockWs()
    alarm_calls: list[int] = []
    rt.ctx.storage.setAlarm = lambda ms: alarm_calls.append(ms)
    await rt.alarm()
    assert alarm_calls


async def test_alarm_reschedules_for_active_lease() -> None:
    """Lines 603-605: no worker_ws, active lease → reschedules alarm."""
    rt = _make_runtime()
    rt.hijack.acquire("alice", 300)
    alarm_calls: list[int] = []
    rt.ctx.storage.setAlarm = lambda ms: alarm_calls.append(ms)
    await rt.alarm()
    assert alarm_calls
