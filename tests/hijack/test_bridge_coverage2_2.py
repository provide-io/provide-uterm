#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage gap tests for hijack/bridge.py (part 2)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from provide.terminal.hijack.bridge import TermBridge

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
