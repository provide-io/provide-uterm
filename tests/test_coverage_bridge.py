#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Targeted tests to cover bridge.py edge/error paths."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from provide.terminal.hijack.bridge import TermBridge, _safe_int


class TestSafeInt:
    def test_non_numeric_string_returns_default(self) -> None:
        assert _safe_int("bad", 80) == 80

    def test_list_returns_default(self) -> None:
        assert _safe_int([1, 2], 25) == 25

    def test_none_returns_default(self) -> None:
        assert _safe_int(None, 42) == 42

    def test_valid_int_string(self) -> None:
        assert _safe_int("123", 0) == 123


class TestSendLoopSerializationError:
    async def test_non_serializable_message_skipped(self) -> None:
        """A message that can't be JSON-serialized is skipped, not fatal."""
        bot = MagicMock()
        bot.worker_id = "test-w"
        bridge = TermBridge.__new__(TermBridge)
        bridge._worker_id = "test-w"
        bridge._running = True
        bridge._send_q = asyncio.Queue()  # type: ignore[assignment]

        ws = AsyncMock()
        sent_payloads: list[str] = []
        ws.send = AsyncMock(side_effect=lambda p: sent_payloads.append(p))

        # Put a bad message (object() is not JSON-serializable), then a good one
        bridge._send_q.put_nowait({"data": object()})  # not JSON serializable
        bridge._send_q.put_nowait({"type": "good"})

        async def stop_after_good():
            while len(sent_payloads) < 1:
                await asyncio.sleep(0.01)
            bridge._running = False
            # Unblock the next _send_q.get() so the while-loop can re-check _running
            bridge._send_q.put_nowait({"type": "_sentinel"})

        await asyncio.gather(
            bridge._send_loop(ws),
            stop_after_good(),
        )
        # Good message was sent; bad one was skipped (sentinel also sent but after _running=False)
        good_payloads = [p for p in sent_payloads if '"good"' in p]
        assert len(good_payloads) == 1
        assert json.loads(good_payloads[0])["type"] == "good"


class TestBridgeErrorHandlers:
    async def test_send_keys_exception_logged(self) -> None:
        """_send_keys catches and logs session.send() failure."""
        bot = MagicMock()
        session = AsyncMock()
        session.send = AsyncMock(side_effect=RuntimeError("connection lost"))
        bot.session = session

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "test"

        # Should not raise
        await bridge._send_keys("hello")

    async def test_request_step_exception_logged(self) -> None:
        """_request_step catches and logs bot.request_step() failure."""
        bot = MagicMock()
        bot.request_step = AsyncMock(side_effect=RuntimeError("step failed"))

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "test"

        await bridge._request_step()

    async def test_set_size_exception_logged(self) -> None:
        """_set_size catches and logs session.set_size() failure."""
        bot = MagicMock()
        session = AsyncMock()
        session.set_size = AsyncMock(side_effect=RuntimeError("resize failed"))
        bot.session = session

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
        bridge._worker_id = "test"

        await bridge._set_size(80, 25)
