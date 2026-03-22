#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for WebSocketSessionConnector — handle_input through set_mode.

Targets survived mutants in server/connectors/websocket.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from provide.terminal.server.connectors.websocket import WebSocketSessionConnector


def _make(config: dict[str, Any] | None = None) -> WebSocketSessionConnector:
    return WebSocketSessionConnector(
        "ws-sess",
        "Test WS",
        config or {"url": "ws://localhost:9999/ws"},
    )


# ---------------------------------------------------------------------------
# handle_input mutations
# ---------------------------------------------------------------------------


class TestHandleInputMutations:
    @pytest.mark.asyncio
    async def test_handle_input_sends_data_to_ws(self) -> None:
        """mutmut_1: must send data when connected."""
        mock_ws = AsyncMock()
        c = _make()
        c._ws = mock_ws
        c._connected = True
        await c.handle_input("test data")
        mock_ws.send.assert_awaited_once_with("test data")

    @pytest.mark.asyncio
    async def test_handle_input_updates_banner(self) -> None:
        """mutmut_4: banner must mention how many chars sent."""
        mock_ws = AsyncMock()
        c = _make()
        c._ws = mock_ws
        c._connected = True
        await c.handle_input("hello")
        assert "5" in c._banner  # 5 chars
        assert "upstream" in c._banner.lower() or "Sent" in c._banner


# ---------------------------------------------------------------------------
# handle_control mutations
# ---------------------------------------------------------------------------


class TestHandleControlMutations:
    @pytest.mark.asyncio
    async def test_pause_sets_paused_true(self) -> None:
        """mutmut_7: pause must set _paused=True."""
        c = _make()
        await c.handle_control("pause")
        assert c._paused is True

    @pytest.mark.asyncio
    async def test_resume_sets_paused_false(self) -> None:
        """mutmut_16-25: resume must set _paused=False."""
        c = _make()
        c._paused = True
        await c.handle_control("resume")
        assert c._paused is False

    @pytest.mark.asyncio
    async def test_pause_banner_mentions_exclusive(self) -> None:
        """mutmut_16: pause banner must mention exclusive control."""
        c = _make()
        await c.handle_control("pause")
        assert "Exclusive control" in c._banner or "exclusive" in c._banner.lower()

    @pytest.mark.asyncio
    async def test_resume_banner_mentions_released(self) -> None:
        """mutmut_17,18,19: resume banner mentions release."""
        c = _make()
        await c.handle_control("resume")
        assert "released" in c._banner.lower() or "Exclusive" in c._banner

    @pytest.mark.asyncio
    async def test_step_banner_mentions_step(self) -> None:
        """mutmut_20,21,22,23,24,25: step banner mentions step."""
        c = _make()
        await c.handle_control("step")
        assert "Step" in c._banner or "step" in c._banner.lower()

    @pytest.mark.asyncio
    async def test_control_returns_snapshot(self) -> None:
        """Control must return a snapshot."""
        c = _make()
        result = await c.handle_control("pause")
        assert len(result) == 1
        assert result[0]["type"] == "snapshot"


# ---------------------------------------------------------------------------
# get_analysis mutations
# ---------------------------------------------------------------------------


class TestGetAnalysisMutations:
    @pytest.mark.asyncio
    async def test_analysis_includes_session_id(self) -> None:
        """mutmut_2: analysis must include session_id."""
        c = WebSocketSessionConnector("my-session", "My WS", {"url": "ws://x"})
        result = await c.get_analysis()
        assert "my-session" in result


# ---------------------------------------------------------------------------
# clear mutations
# ---------------------------------------------------------------------------


class TestClearMutations:
    @pytest.mark.asyncio
    async def test_clear_empties_buffer(self) -> None:
        """mutmut_3,4: screen buffer must be empty string after clear."""
        c = _make()
        c._screen_buffer = "some old data"
        await c.clear()
        assert c._screen_buffer == ""

    @pytest.mark.asyncio
    async def test_clear_updates_banner(self) -> None:
        """mutmut_5,6: banner must be updated after clear."""
        c = _make()
        await c.clear()
        assert "cleared" in c._banner.lower() or "clear" in c._banner.lower()

    @pytest.mark.asyncio
    async def test_clear_returns_snapshot(self) -> None:
        """mutmut_3-6: clear must return snapshot."""
        c = _make()
        result = await c.clear()
        assert len(result) == 1
        assert result[0]["type"] == "snapshot"


# ---------------------------------------------------------------------------
# set_mode mutations
# ---------------------------------------------------------------------------


class TestSetModeMutations:
    @pytest.mark.asyncio
    async def test_set_mode_open_clears_paused(self) -> None:
        """mutmut_13-22: setting open mode must clear paused flag."""
        c = _make()
        c._paused = True
        await c.set_mode("open")
        assert c._paused is False
        assert c._input_mode == "open"

    @pytest.mark.asyncio
    async def test_set_mode_hijack_does_not_change_paused(self) -> None:
        """set_mode hijack must not alter paused state."""
        c = _make()
        c._paused = False
        await c.set_mode("hijack")
        assert c._input_mode == "hijack"
        # paused stays false (no change)
        assert c._paused is False

    @pytest.mark.asyncio
    async def test_set_mode_returns_hello_and_snapshot(self) -> None:
        """mutmut_13-22: set_mode must return [hello, snapshot]."""
        c = _make()
        result = await c.set_mode("open")
        assert len(result) == 2
        assert result[0]["type"] == "worker_hello"
        assert result[1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_set_mode_banner_shows_shared_for_open(self) -> None:
        """mutmut_14-19: banner must say 'Shared input' in open mode."""
        c = _make()
        await c.set_mode("open")
        assert "Shared input" in c._banner

    @pytest.mark.asyncio
    async def test_set_mode_banner_shows_exclusive_for_hijack(self) -> None:
        """mutmut_20-22: banner must say 'Exclusive hijack' in hijack mode."""
        c = _make()
        await c.set_mode("hijack")
        assert "Exclusive hijack" in c._banner

    @pytest.mark.asyncio
    async def test_set_mode_hello_has_correct_input_mode(self) -> None:
        """set_mode must propagate input_mode to hello message."""
        c = _make()
        result = await c.set_mode("hijack")
        assert result[0]["input_mode"] == "hijack"
