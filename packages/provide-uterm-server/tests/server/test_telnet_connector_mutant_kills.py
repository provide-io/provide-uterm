#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for server/connectors/telnet.py."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.defaults import TerminalDefaults
from provide.uterm.server.connectors.telnet import TelnetSessionConnector


def _make_transport(connected: bool = True, recv_data: bytes = b"") -> MagicMock:
    t = MagicMock()
    t.connect = AsyncMock()
    t.disconnect = AsyncMock()
    t.send = AsyncMock()
    t.receive = AsyncMock(return_value=recv_data)
    t.is_connected = MagicMock(return_value=connected)
    return t


def _make_connector(
    session_id: str = "s1",
    display_name: str = "Test",
    config: dict[str, Any] | None = None,
    transport: MagicMock | None = None,
) -> TelnetSessionConnector:
    c = TelnetSessionConnector(session_id, display_name, config or {"host": "127.0.0.1", "port": 2323})
    if transport is not None:
        c._transport = transport
    return c


# ---------------------------------------------------------------------------
# __init__ — config key mutations
# ---------------------------------------------------------------------------


class TestTelnetInitMutations:
    def test_display_name_stored(self) -> None:
        """mut_7: _display_name=None."""
        c = _make_connector(display_name="MySession")
        assert c._display_name == "MySession"

    def test_session_id_stored(self) -> None:
        """mut_6: _session_id=None."""
        c = _make_connector(session_id="unique-id")
        assert c._session_id == "unique-id"

    def test_host_from_config(self) -> None:
        """mut_10/14/15: config.get key changed."""
        c = _make_connector(config={"host": "10.0.0.1", "port": 9999})
        assert c._host == "10.0.0.1"

    def test_port_from_config(self) -> None:
        """mut_18/19/21: port config key mutations."""
        c = _make_connector(config={"host": "10.0.0.1", "port": 9999})
        assert c._port == 9999

    def test_host_default_is_telnet_host(self) -> None:
        """mut_11: default=None instead of TELNET_HOST."""
        c = _make_connector(config={"port": 2323})
        assert c._host == str(TerminalDefaults.TELNET_HOST)

    def test_port_default_is_telnet_remote_port(self) -> None:
        """mut_19: default=None instead of TELNET_REMOTE_PORT."""
        c = _make_connector(config={"host": "127.0.0.1"})
        assert c._port == TerminalDefaults.TELNET_REMOTE_PORT

    def test_transport_not_none(self) -> None:
        """mut_24: _transport=None."""
        c = _make_connector()
        assert c._transport is not None

    def test_connected_false_initially(self) -> None:
        """mut_25: _connected=None."""
        c = _make_connector()
        assert c._connected is False

    def test_input_mode_default_open(self) -> None:
        """Default input_mode is 'open'."""
        c = _make_connector(config={"host": "127.0.0.1", "port": 2323})
        assert c._input_mode == "open"

    def test_paused_false_initially(self) -> None:
        c = _make_connector()
        assert c._paused is False

    def test_received_bytes_zero_initially(self) -> None:
        c = _make_connector()
        assert c._received_bytes == 0

    def test_banner_contains_host_and_port(self) -> None:
        c = _make_connector(config={"host": "192.168.1.1", "port": 9001})
        assert "192.168.1.1" in c._banner
        assert "9001" in c._banner

    def test_unknown_config_key_raises_value_error(self) -> None:
        """Mutation of the error message string."""
        with pytest.raises(ValueError, match="unknown telnet connector_config keys"):
            TelnetSessionConnector("s1", "Test", {"host": "h", "port": 1, "badkey": "v"})


# ---------------------------------------------------------------------------
# start / stop — _connected flag
# ---------------------------------------------------------------------------


class TestStartStop:
    async def test_start_sets_connected_true(self) -> None:
        """mut_5/6 in start: _connected=None or False."""
        t = _make_transport()
        c = _make_connector(transport=t)
        await c.start()
        assert c._connected is True

    async def test_start_calls_connect_with_host_and_port(self) -> None:
        """mut_1/2/3/4 in start: wrong args to transport.connect."""
        t = _make_transport()
        c = _make_connector(config={"host": "10.0.0.1", "port": 7777}, transport=t)
        await c.start()
        t.connect.assert_awaited_once_with("10.0.0.1", 7777)

    async def test_stop_sets_connected_false(self) -> None:
        """mut_1/2 in stop: _connected=None/True."""
        t = _make_transport()
        c = _make_connector(transport=t)
        c._connected = True
        await c.stop()
        assert c._connected is False

    async def test_stop_calls_disconnect(self) -> None:
        t = _make_transport()
        c = _make_connector(transport=t)
        c._connected = True
        await c.stop()
        t.disconnect.assert_awaited_once()


# ---------------------------------------------------------------------------
# _snapshot — key names
# ---------------------------------------------------------------------------


class TestSnapshotKeys:
    async def test_snapshot_type_key(self) -> None:
        """mut_8/9/10/11: 'type' key renamed."""
        c = _make_connector()
        snap = await c.get_snapshot()
        assert "type" in snap
        assert snap["type"] == "snapshot"
        assert "XXtypeXX" not in snap
        assert "TYPE" not in snap

    async def test_snapshot_screen_key(self) -> None:
        """mut_12/13: 'screen' key renamed."""
        c = _make_connector()
        snap = await c.get_snapshot()
        assert "screen" in snap
        assert isinstance(snap["screen"], str)
        assert "XXscreenXX" not in snap

    async def test_snapshot_cursor_key(self) -> None:
        """mut_14/15: 'cursor' key renamed."""
        c = _make_connector()
        snap = await c.get_snapshot()
        assert "cursor" in snap
        assert "XXcursorXX" not in snap
        assert "CURSOR" not in snap

    async def test_snapshot_cursor_x_key(self) -> None:
        """mut_16/17: 'x' key in cursor renamed."""
        c = _make_connector()
        snap = await c.get_snapshot()
        cursor = snap["cursor"]
        assert "x" in cursor
        assert "XXxXX" not in cursor

    async def test_snapshot_cols_is_80(self) -> None:
        c = _make_connector()
        snap = await c.get_snapshot()
        assert snap["cols"] == 80

    async def test_snapshot_rows_is_25(self) -> None:
        c = _make_connector()
        snap = await c.get_snapshot()
        assert snap["rows"] == 25

    async def test_snapshot_has_screen_hash(self) -> None:
        c = _make_connector()
        snap = await c.get_snapshot()
        assert "screen_hash" in snap
        assert len(snap["screen_hash"]) == 16  # first 16 chars of sha256

    async def test_snapshot_screen_hash_is_16_chars(self) -> None:
        """Ensure the truncation to 16 chars is preserved."""
        c = _make_connector()
        snap = await c.get_snapshot()
        assert len(snap["screen_hash"]) == 16


# ---------------------------------------------------------------------------
# _screen — mode display strings
# ---------------------------------------------------------------------------


class TestScreenDisplay:
    def test_open_mode_shows_shared_input(self) -> None:
        """mut_8: == 'open' → != 'open' flips display."""
        c = _make_connector()
        c._input_mode = "open"
        screen = c._screen()
        assert "Shared input" in screen

    def test_hijack_mode_shows_exclusive_hijack(self) -> None:
        c = _make_connector()
        c._input_mode = "hijack"
        screen = c._screen()
        assert "Exclusive hijack" in screen

    def test_paused_shows_paused_for_hijack(self) -> None:
        c = _make_connector()
        c._paused = True
        screen = c._screen()
        assert "Paused for hijack" in screen

    def test_not_paused_shows_live(self) -> None:
        c = _make_connector()
        c._paused = False
        screen = c._screen()
        assert "Live" in screen

    def test_screen_contains_session_id(self) -> None:
        c = _make_connector(session_id="my-session-123")
        screen = c._screen()
        assert "my-session-123" in screen

    def test_screen_contains_display_name(self) -> None:
        c = _make_connector(display_name="Game BBS")
        screen = c._screen()
        assert "Game BBS" in screen


# ---------------------------------------------------------------------------
# _hello — key names
# ---------------------------------------------------------------------------


class TestHelloMessage:
    def test_hello_type_key(self) -> None:
        """mut_1/2: 'type' key renamed."""
        c = _make_connector()
        hello = c._hello()
        assert "type" in hello
        assert hello["type"] == "worker_hello"
        assert "XXtypeXX" not in hello

    def test_hello_input_mode_key(self) -> None:
        """mut_5/6: 'input_mode' key renamed."""
        c = _make_connector()
        hello = c._hello()
        assert "input_mode" in hello
        assert "XXinput_modeXX" not in hello
        assert "INPUT_MODE" not in hello

    def test_hello_ts_key(self) -> None:
        """mut_7/8: 'ts' key renamed."""
        c = _make_connector()
        hello = c._hello()
        assert "ts" in hello
        assert "XXtsXX" not in hello

    def test_hello_input_mode_value(self) -> None:
        c = _make_connector(config={"host": "h", "port": 1, "input_mode": "hijack"})
        hello = c._hello()
        assert hello["input_mode"] == "hijack"


# ---------------------------------------------------------------------------
# poll_messages — byte counting and screen buffer
# ---------------------------------------------------------------------------


class TestPollMessages:
    async def test_poll_increments_received_bytes(self) -> None:
        """Verify received_bytes accumulates correctly."""
        t = _make_transport(recv_data=b"hello")
        c = _make_connector(transport=t)
        c._connected = True
        await c.poll_messages()
        assert c._received_bytes == 5

    async def test_poll_returns_term_and_snapshot(self) -> None:
        t = _make_transport(recv_data=b"data")
        c = _make_connector(transport=t)
        c._connected = True
        msgs = await c.poll_messages()
        types = [m["type"] for m in msgs]
        assert "term" in types
        assert "snapshot" in types

    async def test_poll_screen_buffer_updated(self) -> None:
        t = _make_transport(recv_data=b"hello world")
        c = _make_connector(transport=t)
        c._connected = True
        await c.poll_messages()
        assert "hello world" in c._screen_buffer


# ---------------------------------------------------------------------------
# set_mode
# ---------------------------------------------------------------------------


class TestSetMode:
    async def test_set_open_mode_unpauses(self) -> None:
        c = _make_connector()
        c._paused = True
        await c.set_mode("open")
        assert c._paused is False
        assert c._input_mode == "open"

    async def test_set_hijack_mode_keeps_paused_state(self) -> None:
        c = _make_connector()
        c._paused = False
        await c.set_mode("hijack")
        # hijack mode doesn't force-unpause
        assert c._input_mode == "hijack"

    async def test_set_mode_invalid_raises(self) -> None:
        c = _make_connector()
        with pytest.raises(ValueError, match="invalid mode"):
            await c.set_mode("superuser")

    async def test_set_mode_returns_hello_and_snapshot(self) -> None:
        c = _make_connector()
        msgs = await c.set_mode("open")
        types = [m["type"] for m in msgs]
        assert "worker_hello" in types
        assert "snapshot" in types

    async def test_set_mode_screen_shows_correct_mode(self) -> None:
        c = _make_connector()
        msgs = await c.set_mode("open")
        snap = next(m for m in msgs if m["type"] == "snapshot")
        assert "Shared input" in snap["screen"]

    async def test_set_hijack_mode_screen_shows_exclusive(self) -> None:
        c = _make_connector()
        msgs = await c.set_mode("hijack")
        snap = next(m for m in msgs if m["type"] == "snapshot")
        assert "Exclusive hijack" in snap["screen"]


# ---------------------------------------------------------------------------
# handle_control
# ---------------------------------------------------------------------------


class TestHandleControl:
    async def test_pause_sets_paused(self) -> None:
        c = _make_connector()
        await c.handle_control("pause")
        assert c._paused is True

    async def test_resume_clears_paused(self) -> None:
        c = _make_connector()
        c._paused = True
        await c.handle_control("resume")
        assert c._paused is False

    async def test_unknown_action_returns_snapshot(self) -> None:
        c = _make_connector()
        msgs = await c.handle_control("unknown")
        assert msgs[-1]["type"] == "snapshot"
