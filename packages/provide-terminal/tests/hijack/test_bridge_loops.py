#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for TermBridge loop internals — send loop, recv loop, run loop
integration, and send loop task_done behaviour (Round-9)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any
from unittest.mock import MagicMock, patch

from provide.terminal.control_channel import encode_control, encode_data
from provide.terminal.hijack.bridge import TermBridge

_DLE_STX = "\x10\x02"
_HEADER_LEN = 11  # DLE STX + 8 hex + ':'


def _decode_msg(raw: str) -> dict:
    """Decode a control-channel-framed message to a dict."""
    if raw.startswith(_DLE_STX):
        return json.loads(raw[_HEADER_LEN:])
    return json.loads(raw)


def _ec(msg: dict) -> str:
    """Shorthand for encode_control."""
    return encode_control(msg)


class MockSession:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.sizes: list[tuple[int, int]] = []
        self._watches: list[Any] = []
        self.emulator = MagicMock()
        self.emulator.get_snapshot.return_value = {"screen": "test", "cols": 80, "rows": 25}

    def add_watch(self, fn: Any, *, interval_s: float) -> None:
        self._watches.append(fn)

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def set_size(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))


class MockBot:
    def __init__(self, session: MockSession | None = None) -> None:
        self.session = session
        self.hijacked_calls: list[bool] = []
        self.step_calls: int = 0

    async def set_hijacked(self, enabled: bool) -> None:
        self.hijacked_calls.append(enabled)

    async def request_step(self) -> None:
        self.step_calls += 1


class MockWS:
    """Minimal async WebSocket mock for TermBridge internal loop tests."""

    def __init__(self, messages: list[str] | None = None) -> None:
        self.sent: list[str] = []
        self._messages = list(messages or [])
        self._idx = 0

    async def recv(self) -> str:
        if self._idx >= len(self._messages):
            raise Exception("WebSocket closed")
        msg = self._messages[self._idx]
        self._idx += 1
        return msg

    async def send(self, data: str) -> None:
        self.sent.append(data)


# ---------------------------------------------------------------------------
# _send_loop
# ---------------------------------------------------------------------------


class TestSendLoop:
    async def test_send_loop_sends_queued_message(self) -> None:
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        ws = MockWS()
        # Override send to stop the loop after the first send
        original_send = ws.send

        async def _send_and_stop(data: str) -> None:
            await original_send(data)
            bridge._running = False

        ws.send = _send_and_stop  # type: ignore[method-assign]

        await bridge._send_q.put({"type": "status", "hijacked": False, "ts": 0.0})
        await bridge._send_loop(ws)

        assert len(ws.sent) == 1
        payload = _decode_msg(ws.sent[0])
        assert payload["type"] == "status"

    async def test_send_loop_exits_when_not_running(self) -> None:
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = False  # already stopped

        ws = MockWS()
        # Loop should exit immediately without blocking
        task = asyncio.create_task(bridge._send_loop(ws))
        await asyncio.sleep(0)
        # Queue is empty; loop condition is False so the task should complete quickly
        # Cancel it in case it blocked on get()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert len(ws.sent) == 0


# ---------------------------------------------------------------------------
# _recv_loop
# ---------------------------------------------------------------------------


class TestRecvLoop:
    async def test_recv_loop_snapshot_req_calls_send_snapshot(self) -> None:
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        ws = MockWS([_ec({"type": "snapshot_req"})])
        await bridge._recv_loop(ws)

        # _send_snapshot should have sent a snapshot via ws.send
        assert len(ws.sent) == 1
        payload = _decode_msg(ws.sent[0])
        assert payload["type"] == "snapshot"

    async def test_recv_loop_control_pause(self) -> None:
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        ws = MockWS([_ec({"type": "control", "action": "pause"})])
        await bridge._recv_loop(ws)

        # pause sets hijacked=True; finally block always clears to False on exit
        # so the bot is never left permanently paused after a disconnect.
        assert bot.hijacked_calls == [True, False]

    async def test_recv_loop_control_resume(self) -> None:
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        ws = MockWS([_ec({"type": "control", "action": "resume"})])
        await bridge._recv_loop(ws)

        # resume sets hijacked=False; finally block also calls False (idempotent
        # on the real HijackableMixin, recorded twice only in MockBot).
        assert bot.hijacked_calls == [False, False]

    async def test_recv_loop_control_step(self) -> None:
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        ws = MockWS([_ec({"type": "control", "action": "step"})])
        await bridge._recv_loop(ws)

        assert bot.step_calls == 1

    async def test_recv_loop_input(self) -> None:
        """Regression: raw input data from the WS is forwarded verbatim to the session."""
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        # Input is now sent as raw data (DataChunk), not a JSON envelope.
        # The JS text-input bar sends a real CR; simulate with raw data encoding.
        ws = MockWS([encode_data("hello\r")])
        await bridge._recv_loop(ws)

        assert session.sent == ["hello\r"]

    async def test_recv_loop_resize(self) -> None:
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        ws = MockWS([_ec({"type": "resize", "cols": 132, "rows": 50})])
        await bridge._recv_loop(ws)

        assert session.sizes == [(132, 50)]

    async def test_recv_loop_recv_error_exits(self) -> None:
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        ws = MockWS()  # no messages → recv raises immediately
        await bridge._recv_loop(ws)  # should return cleanly

    async def test_recv_loop_raw_data_then_control(self) -> None:
        """Raw data (non-framed) is treated as input; framed control is processed normally."""
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        # First message: raw text (DataChunk — no session, so ignored)
        # Second message: proper control frame (step)
        ws = MockWS(["some raw data", _ec({"type": "control", "action": "step"})])
        await bridge._recv_loop(ws)

        assert bot.step_calls == 1

    async def test_recv_loop_multiple_messages(self) -> None:
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        ws = MockWS(
            [
                _ec({"type": "control", "action": "pause"}),
                _ec({"type": "control", "action": "resume"}),
                _ec({"type": "control", "action": "step"}),
            ]
        )
        await bridge._recv_loop(ws)

        # pause → True, resume → False, finally → False (idempotent on real impl)
        assert bot.hijacked_calls == [True, False, False]
        assert bot.step_calls == 1


# ---------------------------------------------------------------------------
# Run loop integration
# ---------------------------------------------------------------------------


class TestRunLoop:
    async def test_run_connects_to_ws_server(self) -> None:
        """_run opens a WebSocket to the manager URL and pumps messages."""

        received_frames: list[str] = []

        async def _ws_handler(websocket) -> None:
            try:
                while True:
                    msg = await websocket.recv()
                    received_frames.append(msg)
                    if len(received_frames) >= 1:
                        break
            except Exception:
                pass

        import websockets

        async with websockets.serve(_ws_handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            class _FakeBot:
                session = None

                async def set_hijacked(self, enabled: bool) -> None:
                    pass

                async def request_step(self) -> None:
                    pass

            bot = _FakeBot()
            bridge = TermBridge(bot, "bot1", f"http://127.0.0.1:{port}")
            bridge._send_q.put_nowait({"type": "status", "hijacked": False, "ts": 0.0})
            await bridge.start()
            await asyncio.sleep(0.2)
            await bridge.stop()

        assert len(received_frames) >= 1

    async def test_send_snapshot_exception_suppressed(self) -> None:
        """_send_snapshot catches exceptions from ws.send() silently."""

        class _FakeSession:
            emulator = None

        class _FakeBot:
            session = _FakeSession()

        bot = _FakeBot()
        bridge = TermBridge(bot, "bot1", "http://localhost")

        class _BrokenWs:
            async def send(self, data: object) -> None:
                raise RuntimeError("broken")

        # Should not raise
        await bridge._send_snapshot(_BrokenWs())

    async def test_stop_cancels_child_tasks(self) -> None:
        """stop() must not return until the per-connection pump tasks are cancelled."""
        send_started = asyncio.Event()
        recv_started = asyncio.Event()
        send_cancelled = asyncio.Event()
        recv_cancelled = asyncio.Event()

        class _FakeBot:
            session = None

            async def set_hijacked(self, enabled: bool) -> None:
                pass

            async def request_step(self) -> None:
                pass

        class _FakeConnect:
            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, exc_type, exc, tb) -> None:
                return None

        async def _fake_send_loop(_ws: object) -> None:
            send_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                send_cancelled.set()
                raise

        async def _fake_recv_loop(_ws: object) -> None:
            recv_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                recv_cancelled.set()
                raise

        bridge = TermBridge(_FakeBot(), "bot1", "http://localhost:8000")
        bridge._send_loop = _fake_send_loop  # type: ignore[method-assign]
        bridge._recv_loop = _fake_recv_loop  # type: ignore[method-assign]

        with patch("websockets.connect", return_value=_FakeConnect()):
            await bridge.start()
            await asyncio.wait_for(send_started.wait(), timeout=0.5)
            await asyncio.wait_for(recv_started.wait(), timeout=0.5)
            await bridge.stop()

        assert send_cancelled.is_set()
        assert recv_cancelled.is_set()


# ---------------------------------------------------------------------------
# Round-9 regression: _send_loop calls queue.task_done() after each message
# ---------------------------------------------------------------------------


class TestSendLoopTaskDone:
    """Round-9 fix: _send_loop must call queue.task_done() after each message
    so that queue.join() can be used as a clean-shutdown fence in the future.
    Without task_done(), join() would block forever.
    """

    async def test_task_done_called_after_successful_send(self) -> None:
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        ws = MockWS()
        original_send = ws.send

        async def _send_and_stop(data: str) -> None:
            await original_send(data)
            bridge._running = False

        ws.send = _send_and_stop  # type: ignore[method-assign]

        bridge._send_q.put_nowait({"type": "status", "hijacked": False, "ts": 0.0})
        await bridge._send_loop(ws)

        # queue.join() must complete immediately — task_done() was called.
        try:
            await asyncio.wait_for(bridge._send_q.join(), timeout=0.5)
        except TimeoutError as exc:
            raise AssertionError("task_done() was not called — queue.join() timed out") from exc

    async def test_task_done_called_even_on_send_exception(self) -> None:
        """task_done() must be called even when ws.send() raises, so
        the queue does not accumulate unfinished_tasks.
        """
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        class _BrokenWS:
            sent: list = []

            async def send(self, data: str) -> None:
                raise OSError("network error")

        broken_ws = _BrokenWS()
        bridge._send_q.put_nowait({"type": "term", "data": "x"})

        # _send_loop will raise on the send, but should still call task_done.
        # Stop after one iteration by flagging _running = False in the exception path.
        original_send = broken_ws.send
        call_count = 0

        async def _raise_and_stop(data: str) -> None:
            nonlocal call_count
            call_count += 1
            bridge._running = False
            await original_send(data)

        broken_ws.send = _raise_and_stop  # type: ignore[method-assign]

        # The loop will raise internally; the task itself propagates the OSError.
        import contextlib as _contextlib

        with _contextlib.suppress(OSError):
            await bridge._send_loop(broken_ws)

        # queue.join() must complete immediately — task_done() was called despite the exception.
        try:
            await asyncio.wait_for(bridge._send_q.join(), timeout=0.5)
        except TimeoutError as exc:
            raise AssertionError("task_done() not called after send exception — queue.join() timed out") from exc
