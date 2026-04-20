#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for TelnetWsGateway and _make_process_handler."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from provide.terminal.gateway._gateway import (
    TelnetWsGateway,
    _make_process_handler,
)

# ---------------------------------------------------------------------------
# Async iterator helper
# ---------------------------------------------------------------------------


class _AsyncIter:
    def __init__(self, items: list[Any]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None


def _mock_ws(messages: list[Any] | None = None) -> MagicMock:
    ws = MagicMock()
    ws.__aiter__ = lambda self: _AsyncIter(messages or [])
    ws.send = AsyncMock()
    return ws


def _make_ws_context(ws_mock: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=ws_mock)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# _make_process_handler
# ---------------------------------------------------------------------------


class TestMakeProcessHandler:
    async def test_returns_callable(self) -> None:
        handler = await _make_process_handler("ws://test", "passthrough")
        assert callable(handler)

    async def test_handler_connects_and_pipes(self) -> None:
        handler = await _make_process_handler("ws://test", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.read = AsyncMock(return_value=b"")
        # at_eof: False → enter loop; True → exit after first session ends
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()

        ws_mock = _mock_ws()
        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

        process.exit.assert_called_once_with(0)

    async def test_handler_with_resume_token_and_player_id(self, tmp_path: Path) -> None:
        """Cover player_id included in SSH resume when token has player_id."""
        import json
        tf = tmp_path / "token"
        tf.write_text(json.dumps({"token": "resume_tok", "player_id": 3}))
        handler = await _make_process_handler("ws://test", tf, "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.read = AsyncMock(return_value=b"")
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()

        ws_mock = _mock_ws()
        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

        first = ws_mock.send.call_args_list[0][0][0]
        assert "resume" in first
        assert "player_id" in first
        assert "3" in first

    async def test_handler_with_resume_token(self, tmp_path: Path) -> None:
        tf = tmp_path / "token"
        tf.write_text("resume_tok")
        handler = await _make_process_handler("ws://test", tf, "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.read = AsyncMock(return_value=b"")
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()

        ws_mock = _mock_ws()
        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

        first = ws_mock.send.call_args_list[0][0][0]
        assert "resume" in first

    async def test_handler_exception_calls_exit(self) -> None:
        handler = await _make_process_handler("ws://test", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()
        # at_eof: False → enter loop; True → exit after exception
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.side_effect = OSError("connection refused")

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

        process.exit.assert_called_once_with(0)

    async def test_handler_exit_exception_suppressed(self) -> None:
        handler = await _make_process_handler("ws://test", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock(side_effect=RuntimeError("exit failed"))

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.side_effect = OSError("fail")

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

    async def test_handler_cancels_pending(self) -> None:
        """Cover task.cancel() in _process_handler."""
        from provide.terminal.control_channel import encode_data

        handler = await _make_process_handler("ws://test", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()

        async def slow_read(_n: int = 4096) -> bytes:
            await asyncio.sleep(100)
            return b""

        process.stdin.read = slow_read
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()

        # ws yields one message then ends so _ws_to_ssh finishes quickly
        ws_mock = _mock_ws([encode_data("x")])

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

        process.exit.assert_called_once_with(0)

    async def test_handler_ssh_reconnect_indicator(self) -> None:
        """SSH handler writes reconnect indicator when WS drops but SSH client stays."""
        from provide.terminal.control_channel import encode_data

        handler = await _make_process_handler("ws://test", None, "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()

        async def slow_read(_n: int = 4096) -> bytes:
            await asyncio.sleep(100)
            return b""

        process.stdin.read = slow_read
        # at_eof: False → enter loop 1; False → check after drop (reconnect path);
        # False → enter loop 2; True → exit after second session
        process.stdin.at_eof = MagicMock(side_effect=[False, False, False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()

        call_count = 0

        def make_ws_context_for_call() -> MagicMock:
            nonlocal call_count
            call_count += 1
            ws = _mock_ws([encode_data("x")])  # yields one message then closes
            return _make_ws_context(ws)

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.side_effect = lambda *a, **kw: make_ws_context_for_call()

        with (
            patch.dict("sys.modules", {"websockets": mock_ws_mod}),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await handler(process)

        # Reconnect indicator should have been written to SSH stdout
        written = "".join(call[0][0] for call in process.stdout.write.call_args_list)
        assert "reconnecting" in written
        assert "\x1b7" in written
        assert "\x1b8" in written


        handler = await _make_process_handler("ws://test", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()

        async def slow_read(_n: int = 4096) -> bytes:
            await asyncio.sleep(100)
            return b""

        process.stdin.read = slow_read
        # at_eof: False → enter loop 1; False → check after drop (reconnect path);
        # False → enter loop 2; True → exit after second session
        process.stdin.at_eof = MagicMock(side_effect=[False, False, False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()

        call_count = 0

        def make_ws_context_for_call() -> MagicMock:
            nonlocal call_count
            call_count += 1
            ws = _mock_ws([encode_data("x")])  # yields one message then closes
            return _make_ws_context(ws)

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.side_effect = lambda *a, **kw: make_ws_context_for_call()

        with (
            patch.dict("sys.modules", {"websockets": mock_ws_mod}),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await handler(process)

        # Reconnect indicator should have been written to SSH stdout
        written = "".join(call[0][0] for call in process.stdout.write.call_args_list)
        assert "reconnecting" in written
        assert "\x1b7" in written
        assert "\x1b8" in written


