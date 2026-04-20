#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for do/session_runtime.py — lazy_init, fetch, WebSocket open/message/close/error."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from provide.terminal.cloudflare.do.session_runtime import SessionRuntime

from provide.terminal.control_stream import ControlChunk, ControlStreamDecoder, DataChunk

_KEY = "test-secret-key-32-bytes-minimum!"


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
    types = [_decode_sent(m).get("type") for m in ws.sent]
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
    assert any("ls" in _decode_sent(m, data_frame_type="input").get("data", "") for m in worker_ws.sent)


async def test_websocket_message_raw_bytes() -> None:
    """Lines 417-418: raw bytes → decoded as latin-1."""
    rt = _make_runtime()
    ws = _MockWs(attachment="raw:admin:test-worker")
    rt._register_socket(ws, "raw")
    worker_ws = _MockWs()
    rt.worker_ws = worker_ws
    await rt.webSocketMessage(ws, b"cmd\r")
    assert any("cmd" in _decode_sent(m, data_frame_type="input").get("data", "") for m in worker_ws.sent)


async def test_websocket_message_worker_calls_handle_socket_message() -> None:
    """Lines 423-424: worker socket → handle_socket_message called."""
    rt = _make_runtime()
    ws = _MockWs(attachment="worker:admin:test-worker")
    with patch(
        "provide.terminal.cloudflare.do.session_runtime.handle_socket_message",
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
    types = [_decode_sent(m).get("type") for m in browser.sent]
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
    types = [_decode_sent(m).get("type") for m in browser.sent]
    assert "worker_disconnected" in types


# ---------------------------------------------------------------------------
# request_json
# ---------------------------------------------------------------------------


async def test_request_json_valid() -> None:
    """Lines 460-466: valid JSON body → dict."""
    rt = _make_runtime()
    assert await rt.request_json(_make_mock_request(body='{"key": "value"}')) == {"key": "value"}


async def test_request_json_empty_body() -> None:
    """Lines 461-462: empty body → {}."""
    rt = _make_runtime()
    assert await rt.request_json(_make_mock_request(body="")) == {}


async def test_request_json_non_dict() -> None:
    """Lines 464-465: list JSON → {}."""
    rt = _make_runtime()
    assert await rt.request_json(_make_mock_request(body="[1, 2]")) == {}


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


def _make_mock_request(**kwargs) -> _MockRequest:
    return _MockRequest(**kwargs)
