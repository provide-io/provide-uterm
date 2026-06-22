#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the SSH pump's advertise_redirect toggle (parity with the telnet _pipe_ws)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from provide.uterm.gateway._ssh_handler import _ssh_pump


class _AsyncIter:
    """Wrap a list of items into an async iterator for ``async for message in ws``."""

    def __init__(self, items: list[Any]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None


def _mock_ws(messages: list[Any]) -> MagicMock:
    ws = MagicMock()
    ws.__aiter__ = lambda self: _AsyncIter(messages)
    ws.send = AsyncMock()
    return ws


def _make_ws_context(ws_mock: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=ws_mock)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _idle_process() -> MagicMock:
    """A process whose stdin is already at EOF so _ssh_to_ws ends at once."""
    process = MagicMock()
    process.stdin = AsyncMock()
    process.stdin.read = AsyncMock(return_value="")
    process.stdout = MagicMock()
    return process


async def _run(*, advertise_redirect: bool) -> MagicMock:
    ws_mock = _mock_ws([])
    mock_ws_mod = MagicMock()
    mock_ws_mod.connect.return_value = _make_ws_context(ws_mock)
    with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
        await _ssh_pump(
            _idle_process(),
            "ws://test",
            ws_ssl=None,
            token_holder=[None],
            color_mode="passthrough",
            token_file=None,
            resolved_identity=None,
            upstream_proxy_secret=None,
            redirect_holder=[None],
            advertise_redirect=advertise_redirect,
        )
    return ws_mock


class TestSshPumpAdvertiseRedirect:
    async def test_advertises_supports_redirect_by_default(self) -> None:
        """The SSH gateway advertises its own redirect-follow capability on connect."""
        ws_mock = await _run(advertise_redirect=True)
        # No identity/resume, so the capability hello is the first (only) send.
        first_send = ws_mock.send.call_args_list[0][0][0]
        assert "hello" in first_send
        assert "supports_redirect" in first_send

    async def test_advertise_redirect_can_be_disabled(self) -> None:
        """advertise_redirect=False suppresses the capability hello (plain-server mode)."""
        ws_mock = await _run(advertise_redirect=False)
        assert all("supports_redirect" not in call[0][0] for call in ws_mock.send.call_args_list)
