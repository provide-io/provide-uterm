#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for do/ws_helpers.py — _WsHelperMixin."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from provide.terminal.cloudflare.do.ws_helpers import _WsHelperMixin

from provide.terminal.control_stream import ControlChunk, ControlStreamDecoder


def _make_host(*, jwt_mode: str = "dev") -> _WsHelperMixin:
    class _Host(_WsHelperMixin):
        def __init__(self) -> None:
            self.worker_id = "w1"
            self.worker_ws = None
            self.browser_sockets: dict = {}
            self.raw_sockets: dict = {}
            self.browser_hijack_owner: dict = {}
            self.config = SimpleNamespace(jwt=SimpleNamespace(mode=jwt_mode))

    return _Host()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# ws_key
# ---------------------------------------------------------------------------


def test_ws_key_creates_key() -> None:
    host = _make_host()
    ws = SimpleNamespace()
    key = host.ws_key(ws)
    assert key and "_" in key


def test_ws_key_cached_on_second_call() -> None:
    host = _make_host()
    ws = SimpleNamespace()
    assert host.ws_key(ws) == host.ws_key(ws)


def test_ws_key_different_objects_get_different_keys() -> None:
    host = _make_host()
    assert host.ws_key(SimpleNamespace()) != host.ws_key(SimpleNamespace())


def test_ws_key_exploding_getattr_still_returns_key() -> None:
    """When __getattribute__ raises (not AttributeError), ws_key falls back gracefully."""
    host = _make_host()

    class _Exploding:
        def __getattribute__(self, name: str) -> object:
            raise RuntimeError("explode")

    key = host.ws_key(_Exploding())
    assert key  # non-empty string returned


# ---------------------------------------------------------------------------
# _socket_role — non-string / dict-like attachment branches
# ---------------------------------------------------------------------------


def test_socket_role_plain_string_browser() -> None:
    host = _make_host()
    ws = MagicMock()
    ws.deserializeAttachment.return_value = "browser"
    assert host._socket_role(ws) == "browser"


def test_socket_role_colon_format_worker() -> None:
    host = _make_host()
    ws = MagicMock()
    ws.deserializeAttachment.return_value = "worker:admin:session-1"
    assert host._socket_role(ws) == "worker"


def test_socket_role_dict_attachment_get_method() -> None:
    """Dict attachment uses attachment.get('role')."""
    host = _make_host()
    ws = MagicMock()
    ws.deserializeAttachment.return_value = {"role": "browser"}
    assert host._socket_role(ws) == "browser"


def test_socket_role_dict_unknown_role_falls_back() -> None:
    """Dict attachment with unknown role → 'browser' fallback."""
    host = _make_host()
    ws = MagicMock()
    ws.deserializeAttachment.return_value = {"role": "superadmin"}
    assert host._socket_role(ws) == "browser"


def test_socket_role_object_with_role_attr() -> None:
    """Object attachment with .role attribute but no .get()."""
    host = _make_host()
    ws = MagicMock()
    ws.deserializeAttachment.return_value = SimpleNamespace(role="worker")
    assert host._socket_role(ws) == "worker"


def test_socket_role_to_py_returns_str() -> None:
    """to_py() returning a plain string role."""
    host = _make_host()
    ws = MagicMock()
    att = MagicMock(spec=["to_py"])
    att.to_py.return_value = "raw"
    ws.deserializeAttachment.return_value = att
    assert host._socket_role(ws) == "raw"


def test_socket_role_to_py_returns_dict() -> None:
    """to_py() returning a dict with role key."""
    host = _make_host()
    ws = MagicMock()
    att = MagicMock(spec=["to_py"])
    att.to_py.return_value = {"role": "worker"}
    ws.deserializeAttachment.return_value = att
    assert host._socket_role(ws) == "worker"


def test_socket_role_to_py_raises_falls_back() -> None:
    """When to_py() raises, fall back to 'browser'."""
    host = _make_host()
    ws = MagicMock()
    att = MagicMock(spec=["to_py"])
    att.to_py.side_effect = RuntimeError("boom")
    ws.deserializeAttachment.return_value = att
    assert host._socket_role(ws) == "browser"


def test_socket_role_ut_role_fallback() -> None:
    """_ut_role instance attribute used when all attachment paths fail."""
    host = _make_host()
    ws = MagicMock()
    ws.deserializeAttachment.return_value = {}
    ws._ut_role = "raw"
    assert host._socket_role(ws) == "raw"


# ---------------------------------------------------------------------------
# _socket_browser_role — exception in deserializeAttachment
# ---------------------------------------------------------------------------


def test_socket_browser_role_valid_attachment() -> None:
    host = _make_host()
    ws = MagicMock()
    ws.deserializeAttachment.return_value = "browser:operator:w1"
    assert host._socket_browser_role(ws) == "operator"


def test_socket_browser_role_deserialize_raises_dev_mode() -> None:
    """In dev/none mode, exception → fail-open 'admin'."""
    host = _make_host(jwt_mode="dev")
    ws = MagicMock()
    ws.deserializeAttachment.side_effect = RuntimeError("no attachment")
    assert host._socket_browser_role(ws) == "admin"


def test_socket_browser_role_deserialize_raises_jwt_mode() -> None:
    """In jwt mode, exception → fail-closed 'viewer'."""
    host = _make_host(jwt_mode="jwt")
    ws = MagicMock()
    ws.deserializeAttachment.side_effect = RuntimeError("no attachment")
    assert host._socket_browser_role(ws) == "viewer"


def test_socket_browser_role_ut_browser_role_fallback() -> None:
    """_ut_browser_role instance attribute used when attachment is unreadable."""
    host = _make_host(jwt_mode="jwt")
    ws = MagicMock()
    ws.deserializeAttachment.return_value = "not-a-valid-format"
    ws._ut_browser_role = "operator"
    assert host._socket_browser_role(ws) == "operator"


# ---------------------------------------------------------------------------
# _socket_worker_id — exception in deserializeAttachment
# ---------------------------------------------------------------------------


def test_socket_worker_id_from_attachment() -> None:
    host = _make_host()
    ws = MagicMock()
    ws.deserializeAttachment.return_value = "worker:admin:session-42"
    assert host._socket_worker_id(ws) == "session-42"


def test_socket_worker_id_deserialize_raises_fallback() -> None:
    """Exception → fall back to self.worker_id."""
    host = _make_host()
    ws = MagicMock()
    ws.deserializeAttachment.side_effect = RuntimeError("boom")
    assert host._socket_worker_id(ws) == "w1"


def test_socket_worker_id_short_attachment_fallback() -> None:
    """Fewer than 3 colon parts → fall back to self.worker_id."""
    host = _make_host()
    ws = MagicMock()
    ws.deserializeAttachment.return_value = "worker:admin"
    assert host._socket_worker_id(ws) == "w1"


# ---------------------------------------------------------------------------
# _register_socket / _remove_ws
# ---------------------------------------------------------------------------


def test_register_socket_worker() -> None:
    host = _make_host()
    ws = MagicMock()
    host._register_socket(ws, "worker")
    assert host.worker_ws is ws


def test_register_socket_raw() -> None:
    host = _make_host()
    ws = MagicMock()
    host._register_socket(ws, "raw")
    assert ws in host.raw_sockets.values()


def test_register_and_remove_browser() -> None:
    host = _make_host()
    ws = MagicMock()
    host._register_socket(ws, "browser")
    ws_id = host.ws_key(ws)
    assert ws_id in host.browser_sockets
    host._remove_ws(ws)
    assert ws_id not in host.browser_sockets


def test_remove_ws_clears_worker() -> None:
    host = _make_host()
    ws = MagicMock()
    host.worker_ws = ws
    host._remove_ws(ws)
    assert host.worker_ws is None


# ---------------------------------------------------------------------------
# send_ws / _send_text
# ---------------------------------------------------------------------------


async def test_send_ws_encodes_control_frame() -> None:
    host = _make_host()
    ws = MagicMock()
    ws.send = MagicMock(return_value=None)
    await host.send_ws(ws, {"type": "hello", "v": 1})
    ws.send.assert_called_once()
    payload = ws.send.call_args[0][0]
    decoder = ControlStreamDecoder()
    events = decoder.feed(payload)
    assert len(events) == 1
    assert isinstance(events[0], ControlChunk)
    assert events[0].control == {"type": "hello", "v": 1}


async def test_send_text_awaitable_send() -> None:
    host = _make_host()
    ws = MagicMock()
    ws.send = AsyncMock(return_value=None)
    await host._send_text(ws, "hello world")
    ws.send.assert_called_once_with("hello world")
