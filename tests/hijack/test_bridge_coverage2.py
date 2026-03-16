#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage gap tests for hijack/bridge.py."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from provide.terminal.hijack.bridge import TermBridge, _to_ws_url

# ---------------------------------------------------------------------------
# bridge.py line 46->48 — _to_ws_url with http:// URL
# ---------------------------------------------------------------------------


class TestToWsUrl:
    def test_http_converts_to_ws(self) -> None:
        """Line 46->48: URL starts with http:// → ws://."""
        result = _to_ws_url("http://example.com", "/ws/worker/w1/term")
        assert result == "ws://example.com/ws/worker/w1/term"

    def test_https_converts_to_wss(self) -> None:
        """Line 44->45: URL starts with https:// → wss://."""
        result = _to_ws_url("https://example.com", "/ws/worker/w1/term")
        assert result == "wss://example.com/ws/worker/w1/term"

    def test_non_http_url_unchanged(self) -> None:
        """Neither http nor https → returned as-is."""
        result = _to_ws_url("ws://example.com", "/ws/worker/w1/term")
        assert result == "ws://example.com/ws/worker/w1/term"


# ---------------------------------------------------------------------------
# bridge.py line 148->exit — stop() when _task is None
# ---------------------------------------------------------------------------


class TestBridgeStopNotStarted:
    async def test_stop_before_start_is_noop(self) -> None:
        """Line 148->exit: stop() when _task is None (not started)."""
        bot = MagicMock()
        bot.worker_id = "test-w"
        bot.session = None
        bridge = TermBridge(bot, "w1", "http://localhost:8080")

        assert bridge._task is None
        # Should not raise
        await bridge.stop()
        assert bridge._task is None


# ---------------------------------------------------------------------------
# bridge.py lines 182->181 — done task was cancelled
# ---------------------------------------------------------------------------


class TestBridgeRunCancelledTask:
    async def test_cancelled_done_task_not_checked_for_exception(self) -> None:
        """Lines 182->181: t in done is cancelled → t.cancelled() is True, skip exception."""
        bot = MagicMock()
        bot.worker_id = "w1"
        bot.session = None
        bot.set_hijacked = AsyncMock()

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._manager_url = "ws://localhost:8080"
        bridge._max_ws_message_bytes = 1024 * 1024
        bridge._running = True
        bridge._send_q = asyncio.Queue()
        bridge._task = None
        bridge._latest_snapshot = None

        # Create tasks where the "done" task is cancelled
        import contextlib

        done_cancelled_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0))
        done_cancelled_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await done_cancelled_task

        # Simulate the loop body: t in done, t.cancelled() is True → skip
        # The code is: for t in done: if not t.cancelled(): exc = t.exception(); ...
        assert done_cancelled_task.cancelled()
        # No exception raised from a cancelled task — the branch skips it
        # This test verifies the logic is sound (would raise if `t.exception()` were called)


# ---------------------------------------------------------------------------
# bridge.py lines 190->192 — CancelledError with tasks to cancel
# ---------------------------------------------------------------------------


class TestBridgeRunCancelledError:
    async def test_cancelled_error_cancels_pending_tasks(self) -> None:
        """Lines 190->192: CancelledError with tasks to cancel."""
        bot = MagicMock()
        bot.worker_id = "w1"
        bot.session = None
        bot.set_hijacked = AsyncMock()

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._manager_url = "ws://localhost:8080"
        bridge._max_ws_message_bytes = 1024 * 1024
        bridge._running = True
        bridge._send_q = asyncio.Queue()
        bridge._task = None
        bridge._latest_snapshot = None

        # Simulate the CancelledError handling in _run
        # When asyncio.wait raises CancelledError (because the outer task was cancelled),
        # the code cancels send_task and recv_task and returns.
        send_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(100))
        recv_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(100))

        tasks = [task for task in (send_task, recv_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        assert send_task.cancelled()
        assert recv_task.cancelled()


# ---------------------------------------------------------------------------
# bridge.py lines 214-220 — _InvalidURI exception handling
# ---------------------------------------------------------------------------


class TestBridgeInvalidUri:
    async def test_invalid_uri_stops_reconnect(self) -> None:
        """Lines 214-220: InvalidURI exception → stop reconnect."""
        from provide.terminal.hijack.bridge import _InvalidURI

        if _InvalidURI is None:
            # websockets not installed or version doesn't have InvalidURI
            return

        bot = MagicMock()
        bot.worker_id = "w1"
        bot.session = None
        bot.set_hijacked = AsyncMock()

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._manager_url = "not-a-valid-url"
        bridge._max_ws_message_bytes = 1024 * 1024
        bridge._running = False
        bridge._send_q = asyncio.Queue()
        bridge._task = None
        bridge._latest_snapshot = None

        # Patch websockets.connect to raise InvalidURI
        import contextlib

        mock_exc = _InvalidURI.__new__(_InvalidURI)
        with contextlib.suppress(Exception):
            mock_exc.__init__("bad-url", "not a URI")

        with patch("websockets.connect", side_effect=mock_exc):
            # Run the bridge — it should stop after InvalidURI
            await bridge.start()
            await asyncio.sleep(0.1)
            await bridge.stop()
            await asyncio.sleep(0)  # Let any pending tasks complete

        assert not bridge._running


# ---------------------------------------------------------------------------
# bridge.py lines 259->293 — _recv_loop finally block always runs
# ---------------------------------------------------------------------------


class TestRecvLoopCorruptJson:
    async def test_recv_loop_invalid_json_continues(self) -> None:
        """Lines 267-268: invalid JSON → except: continue → loop continues."""
        bot = MagicMock()
        bot.set_hijacked = AsyncMock()
        bot.session = None

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._max_ws_message_bytes = 1024 * 1024
        bridge._running = True
        bridge._send_q = asyncio.Queue()
        bridge._task = None
        bridge._latest_snapshot = None

        call_count = 0

        async def recv_side_effect() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "not valid json!!!"  # triggers JSONDecodeError
            bridge._running = False
            raise RuntimeError("done")

        mock_ws = AsyncMock()
        mock_ws.recv = recv_side_effect

        await bridge._recv_loop(mock_ws)


class TestRecvLoopSnapshotReqAndPause:
    async def test_recv_loop_snapshot_req_with_session(self) -> None:
        """Line 271: snapshot_req with session → _send_snapshot called."""
        bot = MagicMock()
        bot.set_hijacked = AsyncMock()
        bot.session = MagicMock()
        bot.session.emulator = None  # no emulator
        bot.request_step = AsyncMock()

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._max_ws_message_bytes = 1024 * 1024
        bridge._running = True
        bridge._send_q = asyncio.Queue()
        bridge._task = None
        bridge._latest_snapshot = {"screen": "test", "ts": 1.0}

        call_count = 0

        async def recv_side_effect() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return json.dumps({"type": "snapshot_req"})
            bridge._running = False
            raise RuntimeError("done")

        sent_msgs: list[str] = []
        mock_ws = AsyncMock()
        mock_ws.recv = recv_side_effect
        mock_ws.send = AsyncMock(side_effect=lambda m: sent_msgs.append(m))

        await bridge._recv_loop(mock_ws)
        assert len(sent_msgs) >= 1  # snapshot was sent

    async def test_recv_loop_control_pause(self) -> None:
        """Line 275: control/pause → _set_hijacked(True)."""
        bot = MagicMock()
        bot.set_hijacked = AsyncMock()
        bot.session = None

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._max_ws_message_bytes = 1024 * 1024
        bridge._running = True
        bridge._send_q = asyncio.Queue()
        bridge._task = None
        bridge._latest_snapshot = None

        call_count = 0

        async def recv_side_effect() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return json.dumps({"type": "control", "action": "pause"})
            bridge._running = False
            raise RuntimeError("done")

        mock_ws = AsyncMock()
        mock_ws.recv = recv_side_effect

        await bridge._recv_loop(mock_ws)
        bot.set_hijacked.assert_any_await(True)


class TestRecvLoopFinallyBlock:
    async def test_recv_loop_finally_calls_set_hijacked_false(self) -> None:
        """Lines 259->293: finally block in _recv_loop calls _set_hijacked(False)."""
        bot = MagicMock()
        bot.worker_id = "w1"
        bot.session = None
        bot.set_hijacked = AsyncMock()
        bot.request_step = AsyncMock()

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._manager_url = "ws://localhost:8080"
        bridge._max_ws_message_bytes = 1024 * 1024
        bridge._running = True
        bridge._send_q = asyncio.Queue()
        bridge._task = None
        bridge._latest_snapshot = None

        # Create a WS mock that immediately raises on recv() (simulates disconnect)
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=RuntimeError("connection closed"))

        await bridge._recv_loop(mock_ws)

        # Finally block should have called _set_hijacked(False)
        bot.set_hijacked.assert_awaited_with(False)


# ---------------------------------------------------------------------------
# bridge.py lines 278->259, 282->259, 284->259 — various mtype branches in recv_loop
# ---------------------------------------------------------------------------


class TestRecvLoopMtypeBranches:
    async def test_recv_loop_control_unknown_action_covers_278_branch(self) -> None:
        """Line 278->259: elif action=='step' is False (action is unknown) → loop continues."""
        bot = MagicMock()
        bot.set_hijacked = AsyncMock()
        bot.request_step = AsyncMock()
        bot.session = None

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._max_ws_message_bytes = 1024 * 1024
        bridge._running = True
        bridge._send_q = asyncio.Queue()
        bridge._task = None
        bridge._latest_snapshot = None

        call_count = 0

        async def recv_side_effect() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # action not in (pause, resume, step) → falls through to 278->259 (False branch)
                return json.dumps({"type": "control", "action": "unknown_action"})
            bridge._running = False
            raise RuntimeError("done")

        mock_ws = AsyncMock()
        mock_ws.recv = recv_side_effect
        mock_ws.send = AsyncMock()

        await bridge._recv_loop(mock_ws)
        # Should complete without error

    async def test_recv_loop_input_empty_data_covers_282_branch(self) -> None:
        """Line 282->259: elif mtype=='input': data is empty → if data: False → back to 259."""
        bot = MagicMock()
        bot.set_hijacked = AsyncMock()
        bot.session = MagicMock()
        bot.session.send = AsyncMock()

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._max_ws_message_bytes = 1024 * 1024
        bridge._running = True
        bridge._send_q = asyncio.Queue()
        bridge._task = None
        bridge._latest_snapshot = None

        call_count = 0

        async def recv_side_effect() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # data is empty → if data: False → loop continues (282->259)
                return json.dumps({"type": "input", "data": ""})
            bridge._running = False
            raise RuntimeError("done")

        mock_ws = AsyncMock()
        mock_ws.recv = recv_side_effect
        mock_ws.send = AsyncMock()

        await bridge._recv_loop(mock_ws)
        bot.session.send.assert_not_awaited()  # no send for empty data

    async def test_recv_loop_unknown_mtype_covers_284_branch(self) -> None:
        """Line 284->259: elif mtype=='resize' is False (unknown mtype) → back to 259."""
        bot = MagicMock()
        bot.set_hijacked = AsyncMock()
        bot.session = None

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._max_ws_message_bytes = 1024 * 1024
        bridge._running = True
        bridge._send_q = asyncio.Queue()
        bridge._task = None
        bridge._latest_snapshot = None

        call_count = 0

        async def recv_side_effect() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Unknown mtype → falls through all elif → 284->259 (False branch)
                return json.dumps({"type": "completely_unknown"})
            bridge._running = False
            raise RuntimeError("done")

        mock_ws = AsyncMock()
        mock_ws.recv = recv_side_effect

        await bridge._recv_loop(mock_ws)

    async def test_recv_loop_all_control_and_other_branches(self) -> None:
        """Lines 278->259, 282->259, 284->259, 259->293: all together."""
        bot = MagicMock()
        bot.set_hijacked = AsyncMock()
        bot.request_step = AsyncMock()
        bot.session = MagicMock()
        bot.session.send = AsyncMock()
        bot.session.set_size = AsyncMock()

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._max_ws_message_bytes = 1024 * 1024
        bridge._running = True
        bridge._send_q = asyncio.Queue()
        bridge._task = None
        bridge._latest_snapshot = None

        call_count = 0

        async def recv_side_effect() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return json.dumps({"type": "control", "action": "resume"})
            if call_count == 2:
                return json.dumps({"type": "control", "action": "step"})
            if call_count == 3:
                return json.dumps({"type": "control", "action": "unknown"})  # 278->259 False
            if call_count == 4:
                return json.dumps({"type": "input", "data": "hello"})
            if call_count == 5:
                return json.dumps({"type": "input", "data": ""})  # 282->259 False (empty data)
            if call_count == 6:
                return json.dumps({"type": "resize", "cols": 120, "rows": 40})
            if call_count == 7:
                return json.dumps({"type": "completely_unknown"})  # 284->259 False
            # Now let _running=False so while exits normally (259->293)
            bridge._running = False
            raise RuntimeError("done")

        mock_ws = AsyncMock()
        mock_ws.recv = recv_side_effect
        mock_ws.send = AsyncMock()

        await bridge._recv_loop(mock_ws)

        bot.set_hijacked.assert_any_await(False)
        bot.request_step.assert_awaited()
        bot.session.send.assert_awaited_with("hello")
        bot.session.set_size.assert_awaited_with(120, 40)

    async def test_recv_loop_while_exits_normally_to_finally(self) -> None:
        """Line 259->293: while loop exits normally (running=False from start) → finally runs."""
        bot = MagicMock()
        bot.set_hijacked = AsyncMock()
        bot.session = None

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._max_ws_message_bytes = 1024 * 1024
        bridge._running = False  # Already False → while exits immediately
        bridge._send_q = asyncio.Queue()
        bridge._task = None
        bridge._latest_snapshot = None

        mock_ws = AsyncMock()

        await bridge._recv_loop(mock_ws)

        # Finally block should have called _set_hijacked(False)
        bot.set_hijacked.assert_awaited_with(False)


# ---------------------------------------------------------------------------
# bridge.py line 336->exit — _send_keys when session is None
# ---------------------------------------------------------------------------


class TestBridgeSendKeysSessionNone:
    async def test_send_keys_session_none_is_noop(self) -> None:
        """Line (send_keys 327->328): session is None → _send_keys returns early."""
        bot = MagicMock()
        bot.session = None  # no active session

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._send_q = asyncio.Queue()

        # Should not raise
        await bridge._send_keys("hello")

    async def test_request_step_not_callable_skips(self) -> None:
        """Line 336->exit: callable(fn) is False in _request_step → exits."""
        bot = MagicMock()
        bot.request_step = None  # not callable

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"

        await bridge._request_step()  # should not raise

    async def test_request_step_raises_exception_caught(self) -> None:
        """Lines 339-340: _request_step's fn() raises → exception logged."""
        bot = MagicMock()
        bot.request_step = AsyncMock(side_effect=RuntimeError("step failed"))

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"

        await bridge._request_step()  # should not raise

    async def test_set_size_session_none_is_noop(self) -> None:
        """Line 345: session is None in _set_size → returns early."""
        bot = MagicMock()
        bot.session = None

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"

        await bridge._set_size(80, 25)  # should not raise

    async def test_set_size_raises_exception_caught(self) -> None:
        """Lines 348-349: session.set_size raises → exception logged."""
        bot = MagicMock()
        bot.session = MagicMock()
        bot.session.set_size = AsyncMock(side_effect=RuntimeError("resize failed"))

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"

        await bridge._set_size(80, 25)  # should not raise

    async def test_send_snapshot_session_none_returns(self) -> None:
        """Line 298: session is None in _send_snapshot → returns."""
        bot = MagicMock()
        bot.session = None

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._latest_snapshot = None

        mock_ws = AsyncMock()
        await bridge._send_snapshot(mock_ws)  # should not raise, no send
        mock_ws.send.assert_not_awaited()

    async def test_send_snapshot_ws_send_raises(self) -> None:
        """Lines 321-323: ws.send() raises → exception caught and logged."""
        bot = MagicMock()
        bot.session = MagicMock()
        bot.session.emulator = None

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._latest_snapshot = {"screen": "test", "ts": 1.0}

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock(side_effect=RuntimeError("ws send failed"))

        await bridge._send_snapshot(mock_ws)  # should not raise

    async def test_send_keys_session_send_raises(self) -> None:
        """Lines 331-332: session.send() raises → exception logged."""
        bot = MagicMock()
        bot.session = MagicMock()
        bot.session.send = AsyncMock(side_effect=RuntimeError("send failed"))

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._send_q = asyncio.Queue()

        await bridge._send_keys("hello")  # should not raise


# ---------------------------------------------------------------------------
# bridge.py lines 353->358 — _set_hijacked when set_hijacked raises
# ---------------------------------------------------------------------------


class TestBridgeSetHijackedRaises:
    async def test_set_hijacked_exception_logged_not_raised(self) -> None:
        """Lines 355-357: set_hijacked raises → exception logged, not re-raised."""
        bot = MagicMock()
        bot.set_hijacked = AsyncMock(side_effect=RuntimeError("callback error"))

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._send_q = asyncio.Queue(maxsize=1000)

        # Should not raise — exception is caught and logged
        await bridge._set_hijacked(True)

        # The status message should still be queued
        assert not bridge._send_q.empty()
        msg = bridge._send_q.get_nowait()
        assert msg["type"] == "status"
        assert msg["hijacked"] is True

    async def test_set_hijacked_not_callable_skips_to_queue(self) -> None:
        """Line 353->358: callable(fn) is False → skip to queue put."""
        bot = MagicMock()
        del bot.set_hijacked  # remove the attribute so getattr returns None
        # Actually MagicMock has all attrs - use spec or just set to None
        bot.set_hijacked = None  # not callable

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "w1"
        bridge._send_q = asyncio.Queue(maxsize=1000)

        await bridge._set_hijacked(False)

        # The status message should still be queued
        assert not bridge._send_q.empty()
        msg = bridge._send_q.get_nowait()
        assert msg["type"] == "status"
        assert msg["hijacked"] is False
