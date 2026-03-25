#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for do/session_runtime.py — WebSocket upgrade path and exception branches.

Covers the CF-runtime-specific branches that require mocking:
  - fetch() WebSocket upgrade path (lines 199-265): `from js import WebSocketPair`
  - _lazy_init_worker_id exception branch (lines 180-181)
  - broadcast_to_browsers send-failure cleanup (lines 434-436)
"""

from __future__ import annotations

import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from provide_terminal_cloudflare.do.session_runtime import SessionRuntime

from provide.terminal.control_channel import ControlChannelDecoder, ControlChunk


def _decode_control(raw: str) -> dict:
    decoder = ControlChannelDecoder()
    events = decoder.feed(raw)
    events.extend(decoder.finish())
    ctrl = [e.control for e in events if isinstance(e, ControlChunk)]
    assert len(ctrl) == 1, f"expected 1 control frame, got {len(ctrl)}"
    return ctrl[0]


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_session_runtime_unit.py)
# ---------------------------------------------------------------------------


def _make_ctx(worker_id: str = "test-worker") -> SimpleNamespace:
    conn = sqlite3.connect(":memory:")
    return SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            setAlarm=lambda ms: None,
        ),
        id=SimpleNamespace(name=lambda: worker_id),
        getWebSockets=list,
        acceptWebSocket=lambda ws: None,
    )


def _make_env(mode: str = "dev") -> SimpleNamespace:
    return SimpleNamespace(AUTH_MODE=mode)


def _make_runtime(worker_id: str = "test-worker", mode: str = "dev") -> SessionRuntime:
    return SessionRuntime(_make_ctx(worker_id), _make_env(mode))


class _MockWs:
    """Sync-send WebSocket stub (matching test_session_runtime_unit.py)."""

    def __init__(self, attachment: object = None) -> None:
        self._attachment = attachment
        self.sent: list[str] = []

    def deserializeAttachment(self) -> object:  # noqa: N802
        return self._attachment

    def send(self, data: str) -> None:
        self.sent.append(data)


class _MockRequest:
    """Minimal HTTP request stub."""

    def __init__(
        self,
        url: str = "https://x/worker/test-worker/api/health",
        headers: dict | None = None,
    ) -> None:
        self.url = url
        self._headers = headers or {}
        self.headers = SimpleNamespace(get=lambda k, d=None: self._headers.get(k, d))

    async def text(self) -> str:
        return "{}"


def _ws_pair_mock() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return (js_module_mock, client_ws, server_ws)."""
    client = MagicMock()
    server = MagicMock()
    server.send = MagicMock(return_value=None)
    server.serializeAttachment = MagicMock(return_value=None)
    wp = MagicMock()
    wp.new.return_value.object_values.return_value = (client, server)
    js_mock = MagicMock()
    js_mock.WebSocketPair = wp
    return js_mock, client, server


# ---------------------------------------------------------------------------
# _lazy_init_worker_id — exception branch (lines 180-181)
# ---------------------------------------------------------------------------


def test_lazy_init_worker_id_url_raises_returns_early() -> None:
    """When str(request.url) raises, _lazy_init_worker_id returns without changing worker_id."""
    rt = _make_runtime()
    rt.worker_id = "default"  # trigger the lazy-init path

    class _BadUrl:
        def __str__(self) -> str:
            raise RuntimeError("no url")

    rt._lazy_init_worker_id(SimpleNamespace(url=_BadUrl()))
    assert rt.worker_id == "default"


# ---------------------------------------------------------------------------
# request_json — oversized body guard
# ---------------------------------------------------------------------------


async def test_request_json_oversized_body_returns_empty() -> None:
    """request_json returns {} when body exceeds _MAX_REQUEST_BODY (no crash, no OOM)."""
    from provide_terminal_cloudflare.do._session_runtime_io import _MAX_REQUEST_BODY

    rt = _make_runtime()

    class _BigReq:
        async def text(self) -> str:
            return "x" * (_MAX_REQUEST_BODY + 1)

    result = await rt.request_json(_BigReq())
    assert result == {}


# ---------------------------------------------------------------------------
# fetch() — WebSocket upgrade path (lines 199-265)
# ---------------------------------------------------------------------------


async def test_fetch_websocket_browser_upgrade() -> None:
    """Browser WS upgrade returns 101 with client socket."""
    rt = _make_runtime()
    js_mock, client, server = _ws_pair_mock()

    with patch.dict(sys.modules, {"js": js_mock}):
        resp = await rt.fetch(
            _MockRequest(
                url="https://x/ws/browser/test-worker/term",
                headers={"Upgrade": "websocket"},
            )
        )

    assert resp.status == 101
    assert resp.web_socket is client
    # hello frame sent synchronously in fetch()
    assert server.send.called
    hello = _decode_control(server.send.call_args[0][0])
    assert hello["type"] == "hello"
    assert hello["role"] == "admin"  # dev mode


async def test_fetch_websocket_worker_upgrade() -> None:
    """Worker WS upgrade returns 101 and writes KV registration."""
    rt = _make_runtime()
    js_mock, client, server = _ws_pair_mock()
    mock_kv = AsyncMock()

    with (
        patch.dict(sys.modules, {"js": js_mock}),
        patch("provide_terminal_cloudflare.do.session_runtime.update_kv_session", mock_kv),
    ):
        resp = await rt.fetch(
            _MockRequest(
                url="https://x/ws/worker/test-worker/term",
                headers={"Upgrade": "websocket"},
            )
        )

    assert resp.status == 101
    mock_kv.assert_awaited_once()
    kv_kwargs = mock_kv.call_args.kwargs
    assert kv_kwargs["connected"] is True


async def test_fetch_websocket_raw_upgrade() -> None:
    """Raw WS upgrade returns 101 with socket_role='raw'."""
    rt = _make_runtime()
    js_mock, client, server = _ws_pair_mock()

    with patch.dict(sys.modules, {"js": js_mock}):
        resp = await rt.fetch(
            _MockRequest(
                url="https://x/ws/raw/test-worker/term",
                headers={"Upgrade": "websocket"},
            )
        )

    assert resp.status == 101
    # raw sockets don't get a hello frame
    assert not server.send.called


async def test_fetch_websocket_serialize_attachment_raises_uses_fallback() -> None:
    """When serializeAttachment raises, _ut_role/_ut_browser_role are set on the socket."""
    rt = _make_runtime()
    js_mock, client, server = _ws_pair_mock()
    server.serializeAttachment.side_effect = RuntimeError("no CF runtime")

    with patch.dict(sys.modules, {"js": js_mock}):
        resp = await rt.fetch(
            _MockRequest(
                url="https://x/ws/browser/test-worker/term",
                headers={"Upgrade": "websocket"},
            )
        )

    assert resp.status == 101
    assert server._ut_role == "browser"
    assert server._ut_browser_role == "admin"  # dev mode → admin


async def test_fetch_websocket_worker_kv_raises_still_returns_101() -> None:
    """KV registration failure in worker path is swallowed — 101 still returned."""
    rt = _make_runtime()
    js_mock, client, server = _ws_pair_mock()
    mock_kv = AsyncMock(side_effect=RuntimeError("KV unavailable"))

    with (
        patch.dict(sys.modules, {"js": js_mock}),
        patch("provide_terminal_cloudflare.do.session_runtime.update_kv_session", mock_kv),
    ):
        resp = await rt.fetch(
            _MockRequest(
                url="https://x/ws/worker/test-worker/term",
                headers={"Upgrade": "websocket"},
            )
        )

    assert resp.status == 101


async def test_fetch_websocket_browser_hello_send_raises_still_returns_101() -> None:
    """Hello-frame send failure is swallowed — 101 still returned."""
    rt = _make_runtime()
    js_mock, client, server = _ws_pair_mock()
    server.send.side_effect = RuntimeError("send failed")

    with patch.dict(sys.modules, {"js": js_mock}):
        resp = await rt.fetch(
            _MockRequest(
                url="https://x/ws/browser/test-worker/term",
                headers={"Upgrade": "websocket"},
            )
        )

    assert resp.status == 101


# ---------------------------------------------------------------------------
# broadcast_to_browsers — send-failure cleanup (lines 434-436)
# ---------------------------------------------------------------------------


async def test_broadcast_to_browsers_send_failure_removes_socket() -> None:
    """When send_ws raises for a browser socket, it is removed from browser_sockets."""
    rt = _make_runtime()

    ws = _MockWs(attachment="browser:admin:test-worker")
    ws_id = rt.ws_key(ws)
    rt.browser_sockets[ws_id] = ws
    # Make getWebSockets return the socket (hibernation-recovery path)
    rt.ctx.getWebSockets = lambda: [ws]

    rt.send_ws = AsyncMock(side_effect=RuntimeError("send failed"))

    await rt.broadcast_to_browsers({"type": "test_event"})

    assert ws_id not in rt.browser_sockets


async def test_broadcast_to_browsers_getwebsockets_raises_uses_in_memory() -> None:
    """When ctx.getWebSockets() raises, falls back to in-memory browser_sockets dict."""
    rt = _make_runtime()

    ws = _MockWs(attachment="browser:admin:test-worker")
    ws_id = rt.ws_key(ws)
    rt.browser_sockets[ws_id] = ws
    rt.ctx.getWebSockets = lambda: (_ for _ in ()).throw(RuntimeError("no hibernation API"))

    sent_payloads: list[dict] = []

    async def _capture_send(target_ws, payload):
        sent_payloads.append(payload)

    rt.send_ws = _capture_send

    await rt.broadcast_to_browsers({"type": "ping"})

    assert sent_payloads == [{"type": "ping"}]
