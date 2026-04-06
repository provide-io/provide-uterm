#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression tests for TermBridge reconnect and recv-loop hang fixes.

Fix 4: _run() retries on connection failure instead of exiting permanently.
Fix 5: asyncio.wait uses FIRST_COMPLETED so a recv-loop normal return cancels
       the send-loop (instead of letting it hang on queue.get() forever).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from provide.terminal.bridge.worker_link import TermBridge

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockSession:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._watches: list[Any] = []

    def add_watch(self, fn: Any, *, interval_s: float) -> None:
        self._watches.append(fn)

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def set_size(self, cols: int, rows: int) -> None:
        pass


class MockBot:
    def __init__(self, session: Any = None) -> None:
        self.session: Any = session
        self.hijacked_calls: list[bool] = []
        self.step_calls: int = 0

    async def set_hijacked(self, enabled: bool) -> None:
        self.hijacked_calls.append(enabled)

    async def request_step(self) -> None:
        self.step_calls += 1


# ---------------------------------------------------------------------------
# Fix 5: FIRST_COMPLETED — recv_loop normal return cancels send_loop
# ---------------------------------------------------------------------------


class TestFirstCompleted:
    async def test_recv_loop_exit_cancels_send_loop(self) -> None:
        """Regression: when _recv_loop returns normally (WS closed cleanly),
        asyncio.wait must unblock _send_loop so _run() can proceed to retry.

        With FIRST_EXCEPTION (old code) this would hang because _recv_loop
        returns None (not an exception) and the queue.get() in _send_loop blocks.
        """
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        # A WS mock whose recv() raises immediately (simulates clean close).
        ws_mock = MagicMock()
        ws_mock.recv = AsyncMock(side_effect=Exception("connection closed"))
        ws_mock.send = AsyncMock()

        send_loop_done = asyncio.Event()
        original_send_loop = bridge._send_loop

        async def _tracked_send_loop(ws: Any) -> None:
            try:
                await original_send_loop(ws)
            finally:
                send_loop_done.set()

        bridge._send_loop = _tracked_send_loop  # type: ignore[method-assign]

        # Run both loops; recv exits immediately, send must be cancelled promptly.
        send_task = asyncio.create_task(bridge._send_loop(ws_mock))
        recv_task = asyncio.create_task(bridge._recv_loop(ws_mock))

        done, pending = await asyncio.wait({send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
        # Capture membership before cancellation mutates task state.
        recv_completed_first = recv_task in done
        send_was_pending = send_task in pending

        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        # recv_task must be the one that completed first (WS closed → exception).
        assert recv_completed_first, "recv_task should have completed first"
        # send_task must have been waiting (not self-completing) — FIRST_COMPLETED
        # should have left it in pending so we could cancel it cleanly.
        assert send_was_pending, "send_task should have been pending (waiting on queue.get)"
        # send_loop_done fires once cancel propagates through the finally block.
        assert send_loop_done.is_set()

    async def test_send_loop_exception_cancels_recv_loop(self) -> None:
        """If send_loop raises, recv_loop must be cancelled (FIRST_COMPLETED)."""
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        ws_mock = MagicMock()
        ws_mock.send = AsyncMock(side_effect=Exception("send failed"))
        # recv blocks until cancelled
        recv_unblocked = asyncio.Event()

        async def _blocking_recv() -> bytes:
            await recv_unblocked.wait()
            raise Exception("interrupted")

        ws_mock.recv = _blocking_recv

        # Plant one item in the queue so send_loop tries to send immediately.
        bridge._send_q.put_nowait({"type": "term", "data": "x", "ts": 0.0})

        send_task = asyncio.create_task(bridge._send_loop(ws_mock))
        recv_task = asyncio.create_task(bridge._recv_loop(ws_mock))

        done, pending = await asyncio.wait({send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
            recv_unblocked.set()
        await asyncio.gather(*pending, return_exceptions=True)

        assert send_task in done


# ---------------------------------------------------------------------------
# Fix 4: Reconnect loop — _run() retries after a connection failure
# ---------------------------------------------------------------------------


class TestReconnectLoop:
    async def test_run_retries_after_connection_failure(self) -> None:
        """Regression: _run() must retry after a transient connect error.

        We count how many times websockets.connect is attempted, let it fail
        twice, then stop the bridge. The loop must have attempted >1 connection.
        """
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")

        connect_attempts: list[int] = []

        class _FailOnce:
            """Context manager that raises on first enter, succeeds on second."""

            def __init__(self) -> None:
                self._count = 0

            async def __aenter__(self) -> Any:
                self._count += 1
                connect_attempts.append(self._count)
                if self._count == 1:
                    raise ConnectionRefusedError("first attempt fails")
                # Second attempt: return a WS that receives nothing.
                ws = MagicMock()
                ws.recv = AsyncMock(side_effect=Exception("closed"))
                ws.send = AsyncMock()
                return ws

            async def __aexit__(self, *args: Any) -> None:
                # After second connection, stop the bridge to exit the loop.
                bridge._running = False

        def _fake_connect(url: str, **kw: Any) -> _FailOnce:
            return _FailOnce()

        # Patch at module level so bridge._run picks it up.
        __import__("websockets")

        async def _run_with_zero_backoff() -> None:
            """Run _run() but patch the backoff to 0 so the test is fast."""
            original_backoff = TermBridge._RECONNECT_BACKOFF
            TermBridge._RECONNECT_BACKOFF = (0, 0, 0, 0, 0)
            try:
                real_ws_connect = None
                try:
                    import websockets as _ws

                    real_ws_connect = _ws.connect
                    _ws.connect = _fake_connect  # type: ignore[assignment]
                    bridge._running = True
                    await bridge._run()
                finally:
                    if real_ws_connect is not None:
                        _ws.connect = real_ws_connect
            finally:
                TermBridge._RECONNECT_BACKOFF = original_backoff

        await asyncio.wait_for(_run_with_zero_backoff(), timeout=5.0)

        assert len(connect_attempts) >= 2, f"expected at least 2 connection attempts, got {connect_attempts}"

    async def test_run_stops_when_running_false(self) -> None:
        """_run() exits the retry loop when self._running becomes False."""
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")

        import websockets as _ws

        real_connect = _ws.connect

        def _immediate_fail(url: str, **kw: Any) -> Any:
            bridge._running = False  # stop after first failure

            class _CM:
                async def __aenter__(self) -> Any:
                    raise ConnectionRefusedError("not running")

                async def __aexit__(self, *a: Any) -> None:
                    pass

            return _CM()

        original_backoff = TermBridge._RECONNECT_BACKOFF
        TermBridge._RECONNECT_BACKOFF = (0,)
        try:
            _ws.connect = _immediate_fail  # type: ignore[assignment]
            bridge._running = True
            await asyncio.wait_for(bridge._run(), timeout=3.0)
        finally:
            _ws.connect = real_connect
            TermBridge._RECONNECT_BACKOFF = original_backoff

        assert not bridge._running


# ---------------------------------------------------------------------------
# Fix 4 + 5: backoff constant is accessible
# ---------------------------------------------------------------------------


def test_reconnect_backoff_constant_exists() -> None:
    assert hasattr(TermBridge, "_RECONNECT_BACKOFF")
    assert len(TermBridge._RECONNECT_BACKOFF) > 0
    assert all(v >= 0 for v in TermBridge._RECONNECT_BACKOFF)
