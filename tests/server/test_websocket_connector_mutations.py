#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for WebSocketSessionConnector.

Targets survived mutants in server/connectors/websocket.py.
"""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.server.connectors.websocket import WebSocketSessionConnector


def _make(config: dict[str, Any] | None = None) -> WebSocketSessionConnector:
    return WebSocketSessionConnector(
        "ws-sess",
        "Test WS",
        config or {"url": "ws://localhost:9999/ws"},
    )


# ---------------------------------------------------------------------------
# __init__ mutations (mutmut_7, 12, 13, 14, 25, 26, 30, 31)
# ---------------------------------------------------------------------------


class TestInitMutations:
    def test_display_name_stored_correctly(self) -> None:
        """mutmut_7: display_name must not be set to None."""
        c = _make({"url": "ws://x"})
        assert c._display_name == "Test WS"
        assert c._display_name is not None

    def test_ws_initially_none_not_string(self) -> None:
        """mutmut_12: _ws must start as None, not ''."""
        c = _make()
        assert c._ws is None

    def test_connected_initially_false_not_none(self) -> None:
        """mutmut_13: _connected must start as False, not None."""
        c = _make()
        assert c._connected is False
        assert c._connected is not None

    def test_connected_initially_false_not_true(self) -> None:
        """mutmut_14: _connected must start as False, not True."""
        c = _make()
        assert c._connected is False
        # is_connected() should return False initially
        assert not c.is_connected()

    def test_paused_initially_false_not_none(self) -> None:
        """mutmut_25: _paused must start as False, not None."""
        c = _make()
        assert c._paused is False
        assert c._paused is not None

    def test_paused_initially_false_not_true(self) -> None:
        """mutmut_26: _paused must start as False, not True."""
        c = _make()
        assert c._paused is False

    def test_screen_buffer_initially_empty_string(self) -> None:
        """mutmut_30: _screen_buffer must start as '', not 'XXXX'."""
        c = _make()
        assert c._screen_buffer == ""

    def test_banner_includes_url_not_none(self) -> None:
        """mutmut_31: _banner must include URL, not be None."""
        c = _make({"url": "ws://myhost:1234/ws"})
        assert c._banner is not None
        assert "ws://myhost:1234/ws" in c._banner


# ---------------------------------------------------------------------------
# _screen mutations (mutmut_3..20, 25)
# ---------------------------------------------------------------------------


class TestScreenMutations:
    def test_separator_is_dashes(self) -> None:
        """mutmut_3: separator must be '-', not 'XX-XX'."""
        c = _make({"url": "ws://x"})
        screen = c._screen()
        assert "-" * 60 in screen
        assert "XX-XX" not in screen

    def test_separator_length_is_60(self) -> None:
        """mutmut_4: separator length must be 60, not 61."""
        c = _make({"url": "ws://x"})
        screen = c._screen()
        assert "-" * 60 in screen
        assert "-" * 61 not in screen.replace("-" * 60, "")

    def test_open_mode_shows_shared_input(self) -> None:
        """mutmut_5,6,7: 'Shared input' text must appear when input_mode=='open'."""
        c = _make({"url": "ws://x", "input_mode": "open"})
        screen = c._screen()
        assert "Shared input" in screen
        assert "XXShared inputXX" not in screen
        assert "shared input" not in screen.lower() or "Shared input" in screen
        assert "SHARED INPUT" not in screen

    def test_open_vs_not_open_mode_string(self) -> None:
        """mutmut_8: condition must be == 'open', not != 'open'."""
        c_open = _make({"url": "ws://x", "input_mode": "open"})
        c_hijack = _make({"url": "ws://x", "input_mode": "hijack"})
        assert "Shared input" in c_open._screen()
        assert "Exclusive hijack" in c_hijack._screen()

    def test_exclusive_hijack_shows_in_hijack_mode(self) -> None:
        """mutmut_9,10,11,12,13: 'Exclusive hijack' text must appear when mode is hijack."""
        c = _make({"url": "ws://x", "input_mode": "hijack"})
        screen = c._screen()
        assert "Exclusive hijack" in screen
        assert "XXExclusive hijackXX" not in screen
        assert "exclusive hijack" not in screen.lower() or "Exclusive hijack" in screen
        assert "EXCLUSIVE HIJACK" not in screen

    def test_paused_shows_paused_for_hijack(self) -> None:
        """mutmut_14,15,16: 'Paused for hijack' must appear when paused."""
        c = _make({"url": "ws://x"})
        c._paused = True
        screen = c._screen()
        assert "Paused for hijack" in screen
        assert "XXPaused for hijackXX" not in screen
        assert "PAUSED FOR HIJACK" not in screen

    def test_not_paused_shows_live(self) -> None:
        """mutmut_17,18,19: 'Live' must appear when not paused."""
        c = _make({"url": "ws://x"})
        c._paused = False
        screen = c._screen()
        assert "Live" in screen
        assert "XXLiveXX" not in screen
        assert "LIVE" not in screen

    def test_empty_string_in_header_not_xxxx(self) -> None:
        """mutmut_20: empty string in header must be '', not 'XXXX'."""
        c = _make({"url": "ws://x"})
        screen = c._screen()
        # The header has an empty string which creates a blank line
        assert "XXXX" not in screen

    def test_screen_joined_with_newline(self) -> None:
        """mutmut_25: lines must be joined with '\n', not 'XX\nXX'."""
        c = _make({"url": "ws://x"})
        screen = c._screen()
        assert "XX\nXX" not in screen
        # Screen has proper newlines
        lines = screen.split("\n")
        assert len(lines) > 1


# ---------------------------------------------------------------------------
# _snapshot mutations
# ---------------------------------------------------------------------------


class TestSnapshotMutations:
    def test_snapshot_type_is_snapshot(self) -> None:
        """Snapshot type must be 'snapshot'."""
        c = _make({"url": "ws://x"})
        snap = c._snapshot()
        assert snap["type"] == "snapshot"

    def test_snapshot_screen_key_exists(self) -> None:
        """mutmut_3,4: 'screen' key must exist in snapshot."""
        c = _make({"url": "ws://x"})
        snap = c._snapshot()
        assert "screen" in snap
        assert snap["screen"] is not None

    def test_snapshot_cursor_x_bounded_by_cols(self) -> None:
        """mutmut_6,7: cursor x must be bounded by _COLS-1."""
        c = _make({"url": "ws://x"})
        snap = c._snapshot()
        assert snap["cursor"]["x"] <= 79  # _COLS - 1

    def test_snapshot_cols_is_80(self) -> None:
        """mutmut_16,17: cols must be 80."""
        c = _make({"url": "ws://x"})
        snap = c._snapshot()
        assert snap["cols"] == 80

    def test_snapshot_rows_is_25(self) -> None:
        """mutmut_22,23,24,25: rows must be 25."""
        c = _make({"url": "ws://x"})
        snap = c._snapshot()
        assert snap["rows"] == 25

    def test_snapshot_screen_hash_exists_and_is_string(self) -> None:
        """mutmut_30-33: screen_hash key must exist and be a string."""
        c = _make({"url": "ws://x"})
        snap = c._snapshot()
        assert "screen_hash" in snap
        assert isinstance(snap["screen_hash"], str)
        assert len(snap["screen_hash"]) == 16

    def test_snapshot_screen_hash_matches_screen(self) -> None:
        """screen_hash must be sha256 of screen, not arbitrary value."""
        c = _make({"url": "ws://x"})
        snap = c._snapshot()
        expected = hashlib.sha256(snap["screen"].encode("utf-8")).hexdigest()[:16]
        assert snap["screen_hash"] == expected

    def test_snapshot_cursor_at_end_is_true(self) -> None:
        """mutmut_43,44: cursor_at_end must be True."""
        c = _make({"url": "ws://x"})
        snap = c._snapshot()
        assert snap["cursor_at_end"] is True

    def test_snapshot_has_trailing_space_is_false(self) -> None:
        """mutmut_48-50: has_trailing_space must be False."""
        c = _make({"url": "ws://x"})
        snap = c._snapshot()
        assert snap["has_trailing_space"] is False

    def test_snapshot_prompt_detected_is_ws_stream(self) -> None:
        """mutmut_53-56: prompt_id must be 'ws_stream'."""
        c = _make({"url": "ws://x"})
        snap = c._snapshot()
        assert snap["prompt_detected"]["prompt_id"] == "ws_stream"

    def test_snapshot_ts_is_float(self) -> None:
        """Snapshot ts must be a float."""
        c = _make({"url": "ws://x"})
        snap = c._snapshot()
        assert isinstance(snap["ts"], float)
        assert snap["ts"] > 0


# ---------------------------------------------------------------------------
# _hello mutations
# ---------------------------------------------------------------------------


class TestHelloMutations:
    def test_hello_type_is_worker_hello(self) -> None:
        """mutmut_7: type must be 'worker_hello'."""
        c = _make({"url": "ws://x"})
        hello = c._hello()
        assert hello["type"] == "worker_hello"

    def test_hello_includes_input_mode(self) -> None:
        """mutmut_8: input_mode must be included."""
        c = _make({"url": "ws://x", "input_mode": "hijack"})
        hello = c._hello()
        assert hello["input_mode"] == "hijack"


# ---------------------------------------------------------------------------
# start mutations (mutmut_2, 8)
# ---------------------------------------------------------------------------


class TestStartMutations:
    @pytest.mark.asyncio
    async def test_start_sets_banner_with_connected_message(self) -> None:
        """mutmut_8: banner must say 'Connected to' after start."""
        mock_ws = AsyncMock()
        mock_mod = MagicMock()
        mock_mod.connect = AsyncMock(return_value=mock_ws)
        with patch.dict("sys.modules", {"websockets": mock_mod}):
            c = _make({"url": "ws://localhost:1234/ws"})
            await c.start()
            assert "Connected to" in c._banner
            assert "ws://localhost:1234/ws" in c._banner

    @pytest.mark.asyncio
    async def test_start_sets_connected_true(self) -> None:
        """mutmut_2: _connected must be True after start."""
        mock_ws = AsyncMock()
        mock_mod = MagicMock()
        mock_mod.connect = AsyncMock(return_value=mock_ws)
        with patch.dict("sys.modules", {"websockets": mock_mod}):
            c = _make()
            await c.start()
            assert c._connected is True


# ---------------------------------------------------------------------------
# stop mutations (mutmut_4, 5)
# ---------------------------------------------------------------------------


class TestStopMutations:
    @pytest.mark.asyncio
    async def test_stop_sets_connected_false(self) -> None:
        """mutmut_4: _connected must be False after stop."""
        c = _make()
        c._connected = True
        c._ws = AsyncMock()
        await c.stop()
        assert c._connected is False

    @pytest.mark.asyncio
    async def test_stop_sets_ws_none(self) -> None:
        """mutmut_5: _ws must be None after stop."""
        c = _make()
        c._connected = True
        c._ws = AsyncMock()
        await c.stop()
        assert c._ws is None


# ---------------------------------------------------------------------------
# poll_messages mutations
# ---------------------------------------------------------------------------


class TestPollMessagesMutations:
    @pytest.mark.asyncio
    async def test_poll_returns_empty_when_disconnected(self) -> None:
        """mutmut_1: must return [] when not connected."""
        c = _make()
        result = await c.poll_messages()
        assert result == []

    @pytest.mark.asyncio
    async def test_poll_returns_term_and_snapshot_on_text_data(self) -> None:
        """mutmut_6,9,10: must return [term_msg, snapshot] on text data."""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value="hello world")
        c = _make()
        c._ws = mock_ws
        c._connected = True
        result = await c.poll_messages()
        assert len(result) == 2
        assert result[0]["type"] == "term"
        assert result[0]["data"] == "hello world"
        assert result[1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_poll_tracks_received_bytes_for_text(self) -> None:
        """mutmut_13,14: bytes must be counted as utf-8 length for text."""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value="abc")
        c = _make()
        c._ws = mock_ws
        c._connected = True
        await c.poll_messages()
        assert c._received_bytes == 3

    @pytest.mark.asyncio
    async def test_poll_tracks_received_bytes_for_binary(self) -> None:
        """mutmut_15: bytes must be counted as len(data) for binary."""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=b"\x00\x01\x02")
        c = _make()
        c._ws = mock_ws
        c._connected = True
        await c.poll_messages()
        assert c._received_bytes == 3

    @pytest.mark.asyncio
    async def test_poll_returns_snapshot_on_error(self) -> None:
        """mutmut_29: must return [snapshot] on connection error."""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=ConnectionError("lost"))
        c = _make()
        c._ws = mock_ws
        c._connected = True
        result = await c.poll_messages()
        assert len(result) == 1
        assert result[0]["type"] == "snapshot"
        assert not c.is_connected()

    @pytest.mark.asyncio
    async def test_poll_appends_to_screen_buffer(self) -> None:
        """mutmut_20: screen buffer must accumulate data."""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value="line1")
        c = _make()
        c._ws = mock_ws
        c._connected = True
        await c.poll_messages()
        assert "line1" in c._screen_buffer

    @pytest.mark.asyncio
    async def test_poll_screen_buffer_max_32000(self) -> None:
        """mutmut_23: screen buffer must be capped at 32000 chars."""
        mock_ws = AsyncMock()
        large_data = "x" * 20000
        mock_ws.recv = AsyncMock(return_value=large_data)
        c = _make()
        c._ws = mock_ws
        c._connected = True
        c._screen_buffer = "y" * 20000
        await c.poll_messages()
        assert len(c._screen_buffer) <= 32000

    @pytest.mark.asyncio
    async def test_poll_updates_banner_with_bytes(self) -> None:
        """mutmut_36,37: banner must show received bytes count."""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value="hi")
        c = _make()
        c._ws = mock_ws
        c._connected = True
        await c.poll_messages()
        assert "2" in c._banner  # 2 bytes received
        assert "bytes" in c._banner


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
