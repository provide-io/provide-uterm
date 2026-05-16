#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for DeckMux presence routing in the CF Durable Object.

Coverage:
- presence_update relayed to other browsers
- presence_leave broadcast on disconnect
- presence messages silently dropped when session not presence-enabled
- hello message includes presence_enabled flag
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from provide.uterm.cloudflare.do.session_runtime import SessionRuntime
from provide.uterm.control_channel import ControlChannelDecoder, ControlChunk, encode_control

# ---------------------------------------------------------------------------
# Helpers (mirrors test_session_runtime_unit.py conventions)
# ---------------------------------------------------------------------------


def _decode_sent(raw: str) -> dict:
    decoder = ControlChannelDecoder()
    events = decoder.feed(raw)
    events.extend(decoder.finish())
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ControlChunk)
    return event.control


def _make_ctx(worker_id: str = "test-worker"):
    conn = sqlite3.connect(":memory:")
    return SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            setAlarm=lambda ms: None,
        ),
        id=SimpleNamespace(name=lambda: worker_id),
        getWebSockets=list,  # returns [] — falls back to in-memory dict
    )


def _make_env(mode: str = "dev", **extra) -> SimpleNamespace:
    return SimpleNamespace(AUTH_MODE=mode, **extra)


def _make_runtime(worker_id: str = "test-worker") -> SessionRuntime:
    return SessionRuntime(_make_ctx(worker_id), _make_env())


class _MockWs:
    """Sync-send WebSocket stub."""

    def __init__(self, attachment: object = None) -> None:
        self._attachment = attachment
        self.sent: list[str] = []

    def deserializeAttachment(self) -> object:  # noqa: N802
        return self._attachment

    def serializeAttachment(self, value: object) -> None:  # noqa: N802
        self._attachment = value

    def send(self, data: str) -> None:
        self.sent.append(data)


def _browser_ws(worker_id: str = "test-worker") -> _MockWs:
    return _MockWs(attachment=f"browser:admin:{worker_id}")


def _sent_types(ws: _MockWs) -> list[str]:
    return [_decode_sent(m).get("type") for m in ws.sent]


# ---------------------------------------------------------------------------
# test_hello_includes_presence_flag
# ---------------------------------------------------------------------------


async def test_hello_includes_presence_flag_when_enabled() -> None:
    """webSocketOpen sends presence_enabled=True when meta has presence=True."""
    rt = _make_runtime()
    rt.meta["presence"] = True
    ws = _browser_ws()
    await rt.webSocketOpen(ws)
    hellos = [_decode_sent(m) for m in ws.sent if _decode_sent(m).get("type") == "hello"]
    assert hellos, "hello frame not found"
    assert hellos[0].get("presence_enabled") is True


async def test_hello_includes_presence_flag_when_disabled() -> None:
    """webSocketOpen sends presence_enabled=False when meta has no presence key."""
    rt = _make_runtime()
    ws = _browser_ws()
    await rt.webSocketOpen(ws)
    hellos = [_decode_sent(m) for m in ws.sent if _decode_sent(m).get("type") == "hello"]
    assert hellos, "hello frame not found"
    assert hellos[0].get("presence_enabled") is False


async def test_hello_includes_presence_sync_when_enabled() -> None:
    """webSocketOpen sends presence_sync after hello when presence is enabled."""
    rt = _make_runtime()
    rt.meta["presence"] = True
    ws = _browser_ws()
    await rt.webSocketOpen(ws)
    types = _sent_types(ws)
    assert "presence_sync" in types


async def test_hello_no_presence_sync_when_disabled() -> None:
    """webSocketOpen does NOT send presence_sync when presence is disabled."""
    rt = _make_runtime()
    ws = _browser_ws()
    await rt.webSocketOpen(ws)
    types = _sent_types(ws)
    assert "presence_sync" not in types


# ---------------------------------------------------------------------------
# test_presence_update_relayed
# ---------------------------------------------------------------------------


async def test_presence_update_relayed_to_other_browsers() -> None:
    """presence_update from one browser is relayed to all other browsers."""
    rt = _make_runtime()
    rt.meta["presence"] = True

    ws_a = _browser_ws()
    ws_b = _browser_ws()

    # Register both sockets
    await rt.webSocketOpen(ws_a)
    await rt.webSocketOpen(ws_b)

    # Clear sent frames from connect
    ws_a.sent.clear()
    ws_b.sent.clear()

    # Send presence_update from ws_a
    presence_msg = encode_control({"type": "presence_update", "user_id": "u1", "scroll_line": 5})
    await rt.webSocketMessage(ws_a, presence_msg)

    # ws_b should receive the relay; ws_a should NOT (no echo)
    b_types = _sent_types(ws_b)
    assert "presence_update" in b_types, "presence_update not relayed to ws_b"
    assert "presence_update" not in _sent_types(ws_a), "presence_update echoed to sender"


async def test_queued_input_relayed_to_other_browsers() -> None:
    """queued_input from one browser is relayed to all other browsers."""
    rt = _make_runtime()
    rt.meta["presence"] = True

    ws_a = _browser_ws()
    ws_b = _browser_ws()
    await rt.webSocketOpen(ws_a)
    await rt.webSocketOpen(ws_b)
    ws_a.sent.clear()
    ws_b.sent.clear()

    queued_msg = encode_control({"type": "queued_input", "user_id": "u1", "keys": "ls\n"})
    await rt.webSocketMessage(ws_a, queued_msg)

    b_types = _sent_types(ws_b)
    assert "queued_input" in b_types


# ---------------------------------------------------------------------------
# test_presence_leave_on_disconnect
# ---------------------------------------------------------------------------


async def test_presence_leave_broadcast_on_browser_close() -> None:
    """webSocketClose broadcasts presence_leave to remaining browsers."""
    rt = _make_runtime()
    rt.meta["presence"] = True

    ws_a = _browser_ws()
    ws_b = _browser_ws()
    await rt.webSocketOpen(ws_a)
    await rt.webSocketOpen(ws_b)
    ws_a.sent.clear()
    ws_b.sent.clear()

    # ws_a disconnects
    await rt.webSocketClose(ws_a, code=1000, reason="bye")

    # ws_b should receive presence_leave
    b_types = _sent_types(ws_b)
    assert "presence_leave" in b_types


async def test_no_presence_leave_for_worker_close() -> None:
    """webSocketClose for a worker socket does NOT emit presence_leave."""
    rt = _make_runtime()
    rt.meta["presence"] = True

    ws_b = _browser_ws()
    await rt.webSocketOpen(ws_b)
    ws_b.sent.clear()

    ws_worker = _MockWs(attachment="worker:admin:test-worker")
    rt.worker_ws = ws_worker

    await rt.webSocketClose(ws_worker, code=1000, reason="done")

    # ws_b should receive worker_disconnected, NOT presence_leave
    b_types = _sent_types(ws_b)
    assert "presence_leave" not in b_types


# ---------------------------------------------------------------------------
# test_presence_disabled_session
# ---------------------------------------------------------------------------


async def test_presence_update_dropped_when_disabled() -> None:
    """presence_update is silently dropped when session has no presence flag."""
    rt = _make_runtime()
    # meta does NOT have presence=True

    ws_a = _browser_ws()
    ws_b = _browser_ws()
    await rt.webSocketOpen(ws_a)
    await rt.webSocketOpen(ws_b)
    ws_a.sent.clear()
    ws_b.sent.clear()

    presence_msg = encode_control({"type": "presence_update", "user_id": "u1"})
    await rt.webSocketMessage(ws_a, presence_msg)

    assert "presence_update" not in _sent_types(ws_b), "presence_update should be dropped"


async def test_presence_leave_not_sent_when_disabled() -> None:
    """No presence_leave on disconnect when session is not presence-enabled."""
    rt = _make_runtime()
    # meta does NOT have presence=True

    ws_a = _browser_ws()
    ws_b = _browser_ws()
    await rt.webSocketOpen(ws_a)
    await rt.webSocketOpen(ws_b)
    ws_a.sent.clear()
    ws_b.sent.clear()

    await rt.webSocketClose(ws_a, code=1000, reason="bye")

    assert "presence_leave" not in _sent_types(ws_b)


async def test_control_request_dropped_when_disabled() -> None:
    """control_request is silently dropped when presence is not enabled."""
    rt = _make_runtime()

    ws_a = _browser_ws()
    ws_b = _browser_ws()
    await rt.webSocketOpen(ws_a)
    await rt.webSocketOpen(ws_b)
    ws_a.sent.clear()
    ws_b.sent.clear()

    ctrl_msg = encode_control({"type": "control_request", "user_id": "u1"})
    await rt.webSocketMessage(ws_a, ctrl_msg)

    assert "control_request" not in _sent_types(ws_b)


# ---------------------------------------------------------------------------
# presence_sync payload
# ---------------------------------------------------------------------------


async def test_presence_sync_includes_already_connected_peers() -> None:
    """Second browser gets presence_sync listing the first browser's ID."""
    rt = _make_runtime()
    rt.meta["presence"] = True

    ws_a = _browser_ws()
    ws_b = _browser_ws()
    await rt.webSocketOpen(ws_a)
    ws_b.sent.clear()
    await rt.webSocketOpen(ws_b)

    syncs = [_decode_sent(m) for m in ws_b.sent if _decode_sent(m).get("type") == "presence_sync"]
    assert syncs, "ws_b did not receive presence_sync"
    users = syncs[0].get("users", [])
    assert len(users) == 1, f"expected 1 peer in sync, got {users}"


async def test_presence_sync_config_fields() -> None:
    """presence_sync payload includes config with expected keys."""
    rt = _make_runtime()
    rt.meta["presence"] = True

    ws = _browser_ws()
    await rt.webSocketOpen(ws)

    syncs = [_decode_sent(m) for m in ws.sent if _decode_sent(m).get("type") == "presence_sync"]
    assert syncs
    config = syncs[0].get("config", {})
    assert "auto_transfer_idle_s" in config
    assert "keystroke_queue" in config
