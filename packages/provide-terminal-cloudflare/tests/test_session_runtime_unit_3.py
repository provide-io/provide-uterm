#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for do/session_runtime.py — persist_lease, send helpers, broadcast, push, alarm."""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import patch

from provide.terminal.cloudflare.bridge.hijack import HijackSession
from provide.terminal.cloudflare.do.session_runtime import SessionRuntime
from provide.terminal.cloudflare.state.store import LeaseRecord

from provide.terminal.control_channel import ControlChannelDecoder, ControlChunk, DataChunk

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


# ---------------------------------------------------------------------------
# persist_lease / clear_lease
# ---------------------------------------------------------------------------


def test_persist_lease_none_is_noop() -> None:
    """Line 470: persist_lease(None) -> returns early."""
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
    assert ws.sent and _decode_sent(ws.sent[0])["type"] == "test"


async def test_send_text_async_ws_is_awaited() -> None:
    """Lines 493-495: async ws.send is awaited."""
    rt = _make_runtime()
    ws = _AsyncWs()
    await rt._send_text(ws, "hello")
    assert ws.sent == ["hello"]


async def test_send_hijack_state_no_session() -> None:
    """Lines 498-512: no active session -> hijacked=False."""
    rt = _make_runtime()
    ws = _MockWs()
    await rt.send_hijack_state(ws)
    data = _decode_sent(ws.sent[0])
    assert data["type"] == "hijack_state" and data["hijacked"] is False


async def test_send_hijack_state_with_session_me() -> None:
    """Lines 498-512: active session, matching hijack_id -> owner='me'."""
    rt = _make_runtime()
    session = HijackSession(hijack_id="h1", owner="alice", lease_expires_at=time.time() + 300)
    rt.hijack._session = session
    ws = _MockWs()
    rt.browser_hijack_owner[rt.ws_key(ws)] = "h1"
    await rt.send_hijack_state(ws)
    data = _decode_sent(ws.sent[0])
    assert data["hijacked"] is True and data["owner"] == "me"


async def test_send_hijack_state_with_session_other() -> None:
    """Lines 498-512: active session, different hijack_id -> owner='other'."""
    rt = _make_runtime()
    rt.hijack._session = HijackSession(hijack_id="h1", owner="alice", lease_expires_at=time.time() + 300)
    ws = _MockWs()
    await rt.send_hijack_state(ws)
    data = _decode_sent(ws.sent[0])
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
    """Lines 527-528: no worker_ws -> False."""
    rt = _make_runtime()
    assert await rt.push_worker_control("pause", owner="a", lease_s=60) is False


async def test_push_worker_control_sends_frame() -> None:
    """Lines 529-533: worker_ws present -> control frame sent, returns True."""
    rt = _make_runtime()
    ws = _MockWs()
    rt.worker_ws = ws
    assert await rt.push_worker_control("pause", owner="a", lease_s=60) is True
    assert _decode_sent(ws.sent[0])["type"] == "control"


async def test_push_worker_input_no_ws_returns_false() -> None:
    """Lines 536-537: no worker_ws -> False."""
    rt = _make_runtime()
    assert await rt.push_worker_input("ls\r") is False


async def test_push_worker_input_sends_frame() -> None:
    """Lines 538-539: worker_ws present -> input frame sent, returns True."""
    rt = _make_runtime()
    ws = _MockWs()
    rt.worker_ws = ws
    assert await rt.push_worker_input("ls\r") is True
    assert _decode_sent(ws.sent[0], data_frame_type="input")["type"] == "input"


async def test_push_worker_control_via_ushell() -> None:
    """push_worker_control delegates to _ushell.handle_control when ushell active."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    rt = _make_runtime()
    mock_ushell = SimpleNamespace(handle_control=AsyncMock())
    rt._ushell = mock_ushell
    result = await rt.push_worker_control("pause", owner="a", lease_s=60)
    assert result is True
    mock_ushell.handle_control.assert_awaited_once_with("pause")


async def test_push_worker_input_via_ushell_broadcasts_frames() -> None:
    """push_worker_input routes through ushell and broadcasts returned frames."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    rt = _make_runtime()
    frame = {"type": "term", "data": "hello", "ts": 0.0}
    mock_ushell = SimpleNamespace(handle_input=AsyncMock(return_value=[frame]))
    rt._ushell = mock_ushell

    broadcast_calls: list[dict] = []

    async def _fake_broadcast(f):
        broadcast_calls.append(f)

    with patch.object(rt, "broadcast_to_browsers", side_effect=_fake_broadcast):
        result = await rt.push_worker_input("hi")

    assert result is True
    assert broadcast_calls == [frame]


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
    """Lines 546-547: ctx.getWebSockets() raises -> falls back to browser_sockets."""
    rt = _make_runtime()
    ws = _MockWs(attachment="browser:admin:test-worker")
    rt._register_socket(ws, "browser")

    def bad_get() -> None:
        raise RuntimeError("no")

    rt.ctx.getWebSockets = bad_get
    await rt.broadcast_to_browsers({"type": "test"})
    assert ws.sent


async def test_broadcast_worker_frame_term_to_raw() -> None:
    """Lines 564-565: term frame -> raw sockets get data text."""
    rt = _make_runtime()
    raw_ws = _MockWs()
    rt.raw_sockets[rt.ws_key(raw_ws)] = raw_ws
    await rt.broadcast_worker_frame({"type": "term", "data": "output"})
    assert any("output" in m for m in raw_ws.sent)


async def test_broadcast_worker_frame_snapshot_to_raw() -> None:
    """Lines 566-568: snapshot frame -> raw sockets get screen text."""
    rt = _make_runtime()
    raw_ws = _MockWs()
    rt.raw_sockets[rt.ws_key(raw_ws)] = raw_ws
    await rt.broadcast_worker_frame({"type": "snapshot", "screen": "my-screen"})
    assert any("my-screen" in m for m in raw_ws.sent)


async def test_broadcast_worker_frame_worker_connected() -> None:
    """Lines 569-570: worker_connected -> raw sockets get text."""
    rt = _make_runtime()
    raw_ws = _MockWs()
    rt.raw_sockets[rt.ws_key(raw_ws)] = raw_ws
    await rt.broadcast_worker_frame({"type": "worker_connected"})
    assert any("[worker connected]" in m for m in raw_ws.sent)


async def test_broadcast_worker_frame_worker_disconnected() -> None:
    """Lines 571-572: worker_disconnected -> raw sockets get text."""
    rt = _make_runtime()
    raw_ws = _MockWs()
    rt.raw_sockets[rt.ws_key(raw_ws)] = raw_ws
    await rt.broadcast_worker_frame({"type": "worker_disconnected"})
    assert any("[worker disconnected]" in m for m in raw_ws.sent)


async def test_broadcast_worker_frame_no_text_for_unknown_type() -> None:
    """Lines 574-575: unknown frame type -> raw sockets get nothing."""
    rt = _make_runtime()
    raw_ws = _MockWs()
    rt.raw_sockets[rt.ws_key(raw_ws)] = raw_ws
    await rt.broadcast_worker_frame({"type": "other"})
    assert not raw_ws.sent


# ---------------------------------------------------------------------------
# alarm
# ---------------------------------------------------------------------------


async def test_alarm_releases_expired_lease() -> None:
    """Lines 587-592: expired lease -> released, resume control sent to worker."""
    rt = _make_runtime()
    ws = _MockWs()
    rt.worker_ws = ws
    session = HijackSession(hijack_id="h1", owner="alice", lease_expires_at=time.time() - 1)
    rt.hijack._session = session
    with patch.object(rt.hijack, "_active_session", return_value=session):
        await rt.alarm()
    assert rt.hijack._session is None  # released
    control = [_decode_sent(m) for m in ws.sent if _decode_sent(m).get("type") == "control"]
    assert any(f.get("action") == "resume" for f in control)


async def test_alarm_kv_refresh_when_worker_connected() -> None:
    """Lines 593-602: worker_ws present -> alarm rescheduled."""
    rt = _make_runtime()
    rt.worker_ws = _MockWs()
    alarm_calls: list[int] = []
    rt.ctx.storage.setAlarm = lambda ms: alarm_calls.append(ms)
    await rt.alarm()
    assert alarm_calls


async def test_alarm_reschedules_for_active_lease() -> None:
    """Lines 603-605: no worker_ws, active lease -> reschedules alarm."""
    rt = _make_runtime()
    rt.hijack.acquire("alice", 300)
    alarm_calls: list[int] = []
    rt.ctx.storage.setAlarm = lambda ms: alarm_calls.append(ms)
    await rt.alarm()
    assert alarm_calls
