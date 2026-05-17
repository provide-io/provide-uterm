#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for TelnetWsGateway and _make_process_handler."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from provide.uterm.gateway._ssh_handler import _make_process_handler

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

    async def test_handler_appends_colormode_from_pty_req(self) -> None:
        """SSH client advertising TERM=xterm-256color via pty-req must cause
        the process handler to open the upstream WS at
        ``ws://test?colormode=256`` — same query-param entry point the
        uterm worker expects for browser and telnet paths."""
        handler = await _make_process_handler("ws://test", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.read = AsyncMock(return_value=b"")
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()
        # asyncssh exposes pty-req + env channel data via these methods.
        process.get_terminal_type = MagicMock(return_value="xterm-256color")
        process.get_environment = MagicMock(return_value={})

        ws_mock = _mock_ws()
        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

        # The websockets.connect call must have received the enriched URL.
        actual_url = mock_ws_mod.connect.call_args.args[0]
        assert actual_url == "ws://test?colormode=256"

    async def test_handler_colorterm_env_overrides_term(self) -> None:
        """COLORTERM=truecolor advertised via an SSH env channel must win
        over TERM=xterm."""
        handler = await _make_process_handler("ws://test", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.read = AsyncMock(return_value=b"")
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()
        process.get_terminal_type = MagicMock(return_value="xterm")
        process.get_environment = MagicMock(return_value={"COLORTERM": "truecolor"})

        ws_mock = _mock_ws()
        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

        actual_url = mock_ws_mod.connect.call_args.args[0]
        assert actual_url == "ws://test?colormode=passthrough"

    async def test_handler_no_hint_when_term_unknown(self) -> None:
        """Unknown TERM + no COLORTERM → no colormode query appended (the
        upstream falls through to server_config.color_mode as before)."""
        handler = await _make_process_handler("ws://test", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.read = AsyncMock(return_value=b"")
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()
        process.get_terminal_type = MagicMock(return_value="kitty")
        process.get_environment = MagicMock(return_value={})

        ws_mock = _mock_ws()
        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

        actual_url = mock_ws_mod.connect.call_args.args[0]
        assert actual_url == "ws://test"

    async def test_handler_preserves_existing_query_string(self) -> None:
        """When the base ws_url already carries query params, the colormode
        hint is appended with ``&`` rather than ``?``."""
        handler = await _make_process_handler("ws://test?existing=1", "passthrough")

        process = MagicMock()
        process.stdin = AsyncMock()
        process.stdin.read = AsyncMock(return_value=b"")
        process.stdin.at_eof = MagicMock(side_effect=[False, True])
        process.stdout = MagicMock()
        process.exit = MagicMock()
        process.get_terminal_type = MagicMock(return_value="xterm-256color")
        process.get_environment = MagicMock(return_value={})

        ws_mock = _mock_ws()
        mock_ws_mod = MagicMock()
        mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)

        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await handler(process)

        actual_url = mock_ws_mod.connect.call_args.args[0]
        assert actual_url == "ws://test?existing=1&colormode=256"

    async def test_handler_no_resume_on_fresh_connect(self) -> None:
        """Fresh connection with empty token_holder sends no resume frame."""
        handler = await _make_process_handler("ws://test", "passthrough")

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

        # No resume frame should have been sent (no token in holder)
        ws_mock.send.assert_not_called()

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
        from provide.uterm.control_channel import encode_data

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
        from provide.uterm.control_channel import encode_data

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
