#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for WebSocketTransport (websockets 16.0 ClientConnection).

The websockets 16.0 ``ClientConnection`` exposes ``.send``, ``.recv``,
``.close``, ``.wait_closed`` and ``.state`` (a ``websockets.protocol.State``
enum) — it has NO ``.closed`` attribute. These tests mock the websockets
library accordingly and exercise every branch of the transport for 100%
line + branch coverage.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from provide.uterm.transports.ws_transport import WebSocketTransport


def _make_ws(state: State = State.OPEN) -> MagicMock:
    """Build a fake websockets 16.0 ClientConnection.

    Mirrors the real API surface used by the transport: async ``send``,
    ``recv``, ``close``, ``wait_closed`` plus a settable ``state`` enum.
    Deliberately has NO ``.closed`` attribute so a regression to the old
    ``self._ws.closed`` check would raise AttributeError.
    """
    ws = MagicMock(spec=["send", "recv", "close", "wait_closed", "state"])
    ws.send = AsyncMock()
    ws.recv = AsyncMock()
    ws.close = AsyncMock()
    ws.wait_closed = AsyncMock()
    ws.state = state
    return ws


def _connection_closed() -> ConnectionClosed:
    """Build a minimal valid websockets 16.0 ConnectionClosed instance."""
    return ConnectionClosed(None, None)


# ── connect ──────────────────────────────────────────────────────────────


class TestConnect:
    async def test_connect_with_url_kwarg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _make_ws()
        connect_mock = AsyncMock(return_value=ws)
        monkeypatch.setattr("websockets.connect", connect_mock)

        t = WebSocketTransport()
        await t.connect("ignored", 0, url="wss://example.com/ws")

        connect_mock.assert_awaited_once_with("wss://example.com/ws")
        assert t._url == "wss://example.com/ws"
        assert t._connected is True
        assert t._ws is ws

    async def test_connect_without_url_builds_wss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _make_ws()
        connect_mock = AsyncMock(return_value=ws)
        monkeypatch.setattr("websockets.connect", connect_mock)

        t = WebSocketTransport()
        await t.connect("host.example", 8443)

        connect_mock.assert_awaited_once_with("wss://host.example:8443")
        assert t._connected is True

    async def test_connect_failure_wraps_in_connection_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connect_mock = AsyncMock(side_effect=OSError("boom"))
        monkeypatch.setattr("websockets.connect", connect_mock)

        t = WebSocketTransport()
        with pytest.raises(ConnectionError, match="Failed to connect to wss://host:1"):
            await t.connect("host", 1)

        assert t._connected is False
        assert t._ws is None  # no dangling socket left after a failed connect


# ── is_connected ─────────────────────────────────────────────────────────


class TestIsConnected:
    def test_false_before_connect(self) -> None:
        t = WebSocketTransport()
        assert t.is_connected() is False

    async def test_true_when_open_and_connected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _make_ws(state=State.OPEN)
        monkeypatch.setattr("websockets.connect", AsyncMock(return_value=ws))

        t = WebSocketTransport()
        await t.connect("h", 1)
        assert t.is_connected() is True

    async def test_false_when_state_not_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _make_ws(state=State.OPEN)
        monkeypatch.setattr("websockets.connect", AsyncMock(return_value=ws))

        t = WebSocketTransport()
        await t.connect("h", 1)
        ws.state = State.CLOSED
        assert t.is_connected() is False

    def test_false_when_not_connected_flag(self) -> None:
        # _connected False but _ws present should not raise and return False.
        t = WebSocketTransport()
        t._ws = _make_ws()
        t._connected = False
        assert t.is_connected() is False

    def test_does_not_raise_attribute_error(self) -> None:
        """Regression: the fake ws has no `.closed`; is_connected must use `.state`."""
        t = WebSocketTransport()
        t._ws = _make_ws(state=State.OPEN)
        t._connected = True
        # Would AttributeError if it touched self._ws.closed.
        assert t.is_connected() is True


# ── send ─────────────────────────────────────────────────────────────────


class TestSend:
    async def test_send_not_connected_raises(self) -> None:
        t = WebSocketTransport()
        with pytest.raises(ConnectionError, match="Not connected"):
            await t.send(b"data")

    async def test_send_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _make_ws()
        monkeypatch.setattr("websockets.connect", AsyncMock(return_value=ws))
        t = WebSocketTransport()
        await t.connect("h", 1)

        await t.send(b"hello")
        # WS must emit a TEXT frame: the websockets lib maps str -> TEXT,
        # bytes -> BINARY, so send() decodes to str. Asserting the str (not
        # b"hello") pins TEXT-frame behavior and fails if send() regresses to
        # passing raw bytes (a binary frame the text server would drop).
        ws.send.assert_awaited_once_with("hello")

    async def test_send_decodes_utf8_to_text_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _make_ws()
        monkeypatch.setattr("websockets.connect", AsyncMock(return_value=ws))
        t = WebSocketTransport()
        await t.connect("h", 1)

        # "café" -> UTF-8 bytes; send() must decode back to the str so the frame
        # is TEXT and the codec is UTF-8 (latin-1 would mojibake the é).
        await t.send("café".encode())
        ws.send.assert_awaited_once_with("café")

    async def test_send_connection_closed_disconnects_and_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _make_ws()
        ws.send = AsyncMock(side_effect=_connection_closed())
        monkeypatch.setattr("websockets.connect", AsyncMock(return_value=ws))
        t = WebSocketTransport()
        await t.connect("h", 1)

        with pytest.raises(ConnectionError, match="Connection closed"):
            await t.send(b"hello")
        assert t._ws is None
        assert t._connected is False


# ── receive ──────────────────────────────────────────────────────────────


class TestReceive:
    async def test_receive_not_connected_raises(self) -> None:
        t = WebSocketTransport()
        with pytest.raises(ConnectionError, match="Not connected"):
            await t.receive(4096, 100)

    async def test_receive_str_message_latin1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _make_ws()
        # latin-1 round-trips bytes 0..255; \xff is the canonical check.
        ws.recv = AsyncMock(return_value="A\xff")
        monkeypatch.setattr("websockets.connect", AsyncMock(return_value=ws))
        t = WebSocketTransport()
        await t.connect("h", 1)

        result = await t.receive(4096, 100)
        assert result == b"A\xff"

    async def test_receive_bytes_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _make_ws()
        ws.recv = AsyncMock(return_value=b"\x00\x01\x02")
        monkeypatch.setattr("websockets.connect", AsyncMock(return_value=ws))
        t = WebSocketTransport()
        await t.connect("h", 1)

        result = await t.receive(4096, 100)
        assert result == b"\x00\x01\x02"

    async def test_receive_timeout_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _make_ws()
        ws.recv = AsyncMock(side_effect=TimeoutError)
        monkeypatch.setattr("websockets.connect", AsyncMock(return_value=ws))
        t = WebSocketTransport()
        await t.connect("h", 1)

        result = await t.receive(4096, 10)
        assert result == b""
        # Timeout must NOT disconnect.
        assert t._ws is ws
        assert t._connected is True

    async def test_receive_connection_closed_disconnects_and_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _make_ws()
        ws.recv = AsyncMock(side_effect=_connection_closed())
        monkeypatch.setattr("websockets.connect", AsyncMock(return_value=ws))
        t = WebSocketTransport()
        await t.connect("h", 1)

        with pytest.raises(ConnectionError, match="Connection closed"):
            await t.receive(4096, 100)
        assert t._ws is None
        assert t._connected is False

    async def test_receive_other_exception_disconnects_and_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _make_ws()
        ws.recv = AsyncMock(side_effect=RuntimeError("kaboom"))
        monkeypatch.setattr("websockets.connect", AsyncMock(return_value=ws))
        t = WebSocketTransport()
        await t.connect("h", 1)

        with pytest.raises(ConnectionError, match="WebSocket receive error"):
            await t.receive(4096, 100)
        assert t._ws is None
        assert t._connected is False


# ── disconnect ───────────────────────────────────────────────────────────


class TestDisconnect:
    async def test_disconnect_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _make_ws()
        monkeypatch.setattr("websockets.connect", AsyncMock(return_value=ws))
        t = WebSocketTransport()
        await t.connect("h", 1)

        await t.disconnect()
        assert t._ws is None
        assert t._connected is False
        ws.close.assert_awaited_once()

        # Second call is a no-op (no _ws) and must not raise.
        await t.disconnect()
        assert t._ws is None

    async def test_disconnect_suppresses_close_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _make_ws()
        ws.close = AsyncMock(side_effect=RuntimeError("close failed"))
        monkeypatch.setattr("websockets.connect", AsyncMock(return_value=ws))
        t = WebSocketTransport()
        await t.connect("h", 1)

        # Must swallow the close error and still null _ws.
        await t.disconnect()
        assert t._ws is None
        assert t._connected is False


# ── module import guard ──────────────────────────────────────────────────


class TestImportGuard:
    def test_import_error_raises_helpful_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The top-level try/except converts a missing `websockets` into a
        helpful ImportError. Simulate the failure by reloading the module
        with `websockets` hidden from the import machinery."""
        import builtins
        import importlib

        real_import = builtins.__import__

        def _fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "websockets" or name.startswith("websockets."):
                raise ImportError("No module named 'websockets'")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", _fake_import)

        import provide.uterm.transports.ws_transport as mod

        with pytest.raises(ImportError, match="websockets is required"):
            importlib.reload(mod)

        # Restore a clean, importable module for the rest of the suite.
        monkeypatch.undo()
        importlib.reload(mod)
