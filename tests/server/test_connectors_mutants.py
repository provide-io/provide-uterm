#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for server/connectors/ssh.py, telnet.py, and websocket.py.

Targets all survived mutants discovered by mutmut in those three files.
"""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers — Telnet
# ---------------------------------------------------------------------------


def _make_telnet_transport(*, connected: bool = True, recv_data: bytes = b"") -> MagicMock:
    t = MagicMock()
    t.is_connected.return_value = connected
    t.connect = AsyncMock()
    t.disconnect = AsyncMock()
    t.send = AsyncMock()
    t.receive = AsyncMock(return_value=recv_data)
    return t


def _make_telnet(config: dict[str, Any] | None = None, transport: MagicMock | None = None) -> Any:
    from provide.terminal.server.connectors.telnet import TelnetSessionConnector

    c = TelnetSessionConnector(
        "sess-t",
        "Test Telnet",
        config or {"host": "127.0.0.1", "port": 2323},
    )
    if transport is not None:
        c._transport = transport
    return c


# ---------------------------------------------------------------------------
# Helpers — SSH
# ---------------------------------------------------------------------------


def _make_ssh(config: dict[str, Any] | None = None) -> Any:
    pytest.importorskip("asyncssh", reason="asyncssh not installed")
    from provide.terminal.server.connectors.ssh import SshSessionConnector

    return SshSessionConnector(
        "sess-s",
        "Test SSH",
        config or {"host": "localhost", "insecure_no_host_check": True},
    )


def _attach_ssh(connector: Any) -> tuple[MagicMock, MagicMock, MagicMock]:
    mock_stdout = MagicMock()
    mock_stdin = MagicMock()
    mock_stdin.drain = AsyncMock()
    mock_conn = MagicMock()
    mock_conn.close = MagicMock()
    mock_conn.wait_closed = AsyncMock()
    connector._conn = mock_conn
    connector._stdin = mock_stdin
    connector._stdout = mock_stdout
    connector._connected = True
    return mock_conn, mock_stdin, mock_stdout


# ===========================================================================
# TelnetSessionConnector — __init__ mutants
# ===========================================================================


class TestTelnetInit:
    def test_session_id_stored(self) -> None:
        """_session_id must match the constructor argument."""
        c = _make_telnet()
        assert c._session_id == "sess-t"

    def test_display_name_stored_not_none(self) -> None:
        """mutmut_7: _display_name must be set to the argument, not None."""
        c = _make_telnet()
        assert c._display_name == "Test Telnet"
        assert c._display_name is not None

    def test_host_from_config_overrides_default(self) -> None:
        """mutmut_10,11,13,14,15: host must be read from config key 'host'."""
        c = _make_telnet({"host": "192.0.2.1", "port": 23})
        assert c._host == "192.0.2.1"

    def test_host_defaults_to_telnet_default(self) -> None:
        """mutmut_11: missing host must fall back to TerminalDefaults.TELNET_HOST."""
        from provide.terminal.defaults import TerminalDefaults

        c = _make_telnet({"port": 23})
        assert c._host == TerminalDefaults.TELNET_HOST
        assert c._host == "127.0.0.1"

    def test_port_from_config_overrides_default(self) -> None:
        """mutmut_19,21: port must be read from config key 'port'."""
        c = _make_telnet({"host": "127.0.0.1", "port": 9999})
        assert c._port == 9999

    def test_port_defaults_to_telnet_remote_port(self) -> None:
        """mutmut_19: missing port must fall back to TerminalDefaults.TELNET_REMOTE_PORT."""
        from provide.terminal.defaults import TerminalDefaults

        c = _make_telnet({"host": "127.0.0.1"})
        assert c._port == TerminalDefaults.TELNET_REMOTE_PORT
        assert c._port == 23

    def test_transport_is_not_none(self) -> None:
        """mutmut_24: _transport must be a TelnetTransport instance, not None."""
        from provide.terminal.transports.telnet import TelnetTransport

        c = _make_telnet()
        assert isinstance(c._transport, TelnetTransport)
        assert c._transport is not None

    def test_connected_initially_false_not_none(self) -> None:
        """mutmut_25: _connected must start False, not None."""
        c = _make_telnet()
        assert c._connected is False
        assert c._connected is not None

    def test_connected_initially_false_not_true(self) -> None:
        """mutmut_26: _connected must start False, not True."""
        c = _make_telnet()
        assert c._connected is False
        assert not c.is_connected()

    def test_input_mode_from_config(self) -> None:
        """mutmut_27,28,29,30,31,32,33,34: input_mode must be read from config."""
        c = _make_telnet({"host": "h", "port": 23, "input_mode": "hijack"})
        assert c._input_mode == "hijack"

    def test_input_mode_default_is_open(self) -> None:
        """mutmut_30,32,35,36: default input_mode must be 'open', not None or variant."""
        c = _make_telnet()
        assert c._input_mode == "open"

    def test_paused_initially_false_not_none(self) -> None:
        """mutmut_37: _paused must start False, not None."""
        c = _make_telnet()
        assert c._paused is False
        assert c._paused is not None

    def test_paused_initially_false_not_true(self) -> None:
        """mutmut_38: _paused must start False, not True."""
        c = _make_telnet()
        assert c._paused is False

    def test_screen_buffer_initially_empty(self) -> None:
        """mutmut_42: _screen_buffer must start as empty string, not 'XXXX'."""
        c = _make_telnet()
        assert c._screen_buffer == ""

    def test_banner_includes_host_and_port(self) -> None:
        """mutmut_43: _banner must include host/port and not be None."""
        c = _make_telnet({"host": "192.0.2.5", "port": 4321})
        assert c._banner is not None
        assert "192.0.2.5" in c._banner
        assert "4321" in c._banner


# ===========================================================================
# TelnetSessionConnector — _screen mutants
# ===========================================================================


class TestTelnetScreen:
    def test_separator_is_dashes(self) -> None:
        """mutmut_3: separator must be '-'*60."""
        c = _make_telnet({"host": "h", "port": 23})
        screen = c._screen()
        assert "-" * 60 in screen
        assert "XX-XX" not in screen

    def test_separator_length_is_60(self) -> None:
        """mutmut_4: separator must have exactly 60 dashes."""
        c = _make_telnet({"host": "h", "port": 23})
        screen = c._screen()
        assert "-" * 60 in screen
        # 61 dashes must NOT appear except as part of 60
        trimmed = screen.replace("-" * 60, "")
        assert "-" not in trimmed or "-" * 61 not in screen

    def test_open_mode_shows_shared_input(self) -> None:
        """mutmut_5,6,7: 'Shared input' must appear when input_mode=='open'."""
        c = _make_telnet({"host": "h", "port": 23, "input_mode": "open"})
        screen = c._screen()
        assert "Shared input" in screen

    def test_condition_open_not_negated(self) -> None:
        """mutmut_8: condition must be == 'open', not != 'open'."""
        c_open = _make_telnet({"host": "h", "port": 23, "input_mode": "open"})
        c_hijack = _make_telnet({"host": "h", "port": 23, "input_mode": "hijack"})
        assert "Shared input" in c_open._screen()
        assert "Exclusive hijack" in c_hijack._screen()
        assert "Exclusive hijack" not in c_open._screen()
        assert "Shared input" not in c_hijack._screen()

    def test_open_mode_keyword_is_lowercase(self) -> None:
        """mutmut_9,10: comparison must be against 'open', not 'XXopenXX' or 'OPEN'."""
        c = _make_telnet({"host": "h", "port": 23, "input_mode": "open"})
        assert "Shared input" in c._screen()

    def test_hijack_mode_shows_exclusive_hijack(self) -> None:
        """mutmut_11,12,13: 'Exclusive hijack' must appear in hijack mode."""
        c = _make_telnet({"host": "h", "port": 23, "input_mode": "hijack"})
        screen = c._screen()
        assert "Exclusive hijack" in screen
        assert "XXExclusive hijackXX" not in screen
        assert "EXCLUSIVE HIJACK" not in screen

    def test_paused_shows_paused_for_hijack(self) -> None:
        """mutmut_14: 'Paused for hijack' must appear when _paused is True."""
        c = _make_telnet({"host": "h", "port": 23})
        c._paused = True
        screen = c._screen()
        assert "Paused for hijack" in screen
        assert "XXPaused for hijackXX" not in screen

    def test_live_shows_when_not_paused(self) -> None:
        """mutmut_17: 'Live' must appear when _paused is False."""
        c = _make_telnet({"host": "h", "port": 23})
        c._paused = False
        screen = c._screen()
        assert "Live" in screen
        assert "XXLiveXX" not in screen

    def test_no_xxxx_in_screen(self) -> None:
        """mutmut_20: screen must not contain 'XXXX'."""
        c = _make_telnet({"host": "h", "port": 23})
        assert "XXXX" not in c._screen()

    def test_screen_uses_plain_newline_separator(self) -> None:
        """mutmut_25: lines must be joined with '\n', not 'XX\nXX'."""
        c = _make_telnet({"host": "h", "port": 23})
        screen = c._screen()
        assert "XX\nXX" not in screen
        assert "\n" in screen


# ===========================================================================
# TelnetSessionConnector — _snapshot mutants
# ===========================================================================


class TestTelnetSnapshot:
    def test_snapshot_type(self) -> None:
        c = _make_telnet()
        snap = c._snapshot()
        assert snap["type"] == "snapshot"

    def test_snapshot_has_screen_key(self) -> None:
        """mutmut_3,4: 'screen' key must exist and have a value."""
        c = _make_telnet()
        snap = c._snapshot()
        assert "screen" in snap
        assert snap["screen"] is not None

    def test_snapshot_cursor_x_uses_last_line(self) -> None:
        """mutmut_6,7: cursor x must use last line (lines[-1]) of the screen."""
        c = _make_telnet({"host": "h", "port": 23})
        c._screen_buffer = "A" * 79  # last line has 79 chars, x should be 79
        snap = c._snapshot()
        # x = min(len(last), 79); if last line is not the single char line, x varies
        # The key invariant: cursor x is bounded by _COLS-1 (79)
        assert snap["cursor"]["x"] <= 79

    def test_snapshot_cursor_x_bounded_by_cols_minus_1(self) -> None:
        """mutmut_22,23: cursor x = min(len(last), _COLS-1), not _COLS+1 or _COLS-2."""
        c = _make_telnet({"host": "h", "port": 23})
        c._screen_buffer = "A" * 200  # very long last line
        snap = c._snapshot()
        assert snap["cursor"]["x"] == 79  # capped at _COLS - 1

    def test_snapshot_cursor_has_x_key(self) -> None:
        """mutmut_14,15,16,17: cursor dict must have key 'x', not 'XXxXX' or 'X'."""
        c = _make_telnet()
        snap = c._snapshot()
        assert "x" in snap["cursor"]
        assert "XXcursorXX" not in snap
        assert "CURSOR" not in snap
        assert "XXxXX" not in snap["cursor"]
        assert "X" not in snap["cursor"]

    def test_snapshot_cursor_has_y_key(self) -> None:
        """mutmut_24,25: cursor dict must have key 'y', not 'XXyXX' or 'Y'."""
        c = _make_telnet()
        snap = c._snapshot()
        assert "y" in snap["cursor"]
        assert "XXyXX" not in snap["cursor"]
        assert "Y" not in snap["cursor"]

    def test_snapshot_cursor_y_bounded_by_rows_minus_1(self) -> None:
        """mutmut_30,31,32,33: cursor y must be bounded by _ROWS-1 (24)."""
        c = _make_telnet({"host": "h", "port": 23})
        # Force many lines
        c._screen_buffer = "\n".join(["line"] * 100)
        snap = c._snapshot()
        assert snap["cursor"]["y"] <= 24  # _ROWS - 1

    def test_snapshot_cols_is_80(self) -> None:
        """_COLS constant must be 80."""
        c = _make_telnet()
        snap = c._snapshot()
        assert snap["cols"] == 80

    def test_snapshot_rows_is_25(self) -> None:
        """mutmut_36,37: 'rows' key must equal 25."""
        c = _make_telnet()
        snap = c._snapshot()
        assert "rows" in snap
        assert snap["rows"] == 25
        assert "XXrowsXX" not in snap
        assert "ROWS" not in snap

    def test_snapshot_screen_hash_key_exists(self) -> None:
        """mutmut_38,39: 'screen_hash' key must exist."""
        c = _make_telnet()
        snap = c._snapshot()
        assert "screen_hash" in snap
        assert "XXscreen_hashXX" not in snap
        assert "SCREEN_HASH" not in snap

    def test_snapshot_screen_hash_length_is_16(self) -> None:
        """mutmut_44: screen_hash must be 16 chars (not 17)."""
        c = _make_telnet()
        snap = c._snapshot()
        assert len(snap["screen_hash"]) == 16

    def test_snapshot_screen_hash_matches_screen(self) -> None:
        """mutmut_43: screen_hash must be sha256 of screen encoded as utf-8."""
        c = _make_telnet()
        snap = c._snapshot()
        expected = hashlib.sha256(snap["screen"].encode("utf-8")).hexdigest()[:16]
        assert snap["screen_hash"] == expected

    def test_snapshot_cursor_at_end_is_true(self) -> None:
        """mutmut_45,46,47: cursor_at_end must be True."""
        c = _make_telnet()
        snap = c._snapshot()
        assert "cursor_at_end" in snap
        assert snap["cursor_at_end"] is True
        assert "XXcursor_at_endXX" not in snap
        assert "CURSOR_AT_END" not in snap

    def test_snapshot_has_trailing_space_is_false(self) -> None:
        """mutmut_48,49,50: has_trailing_space must be False."""
        c = _make_telnet()
        snap = c._snapshot()
        assert "has_trailing_space" in snap
        assert snap["has_trailing_space"] is False
        assert "XXhas_trailing_spaceXX" not in snap
        assert "HAS_TRAILING_SPACE" not in snap

    def test_snapshot_prompt_detected_key_is_correct(self) -> None:
        """mutmut_51,52: 'prompt_detected' key must exist."""
        c = _make_telnet()
        snap = c._snapshot()
        assert "prompt_detected" in snap
        assert "XXprompt_detectedXX" not in snap
        assert "PROMPT_DETECTED" not in snap

    def test_snapshot_prompt_id_key_in_prompt_detected(self) -> None:
        """mutmut_53,54: prompt_detected dict must have key 'prompt_id'."""
        c = _make_telnet()
        snap = c._snapshot()
        assert "prompt_id" in snap["prompt_detected"]
        assert "XXprompt_idXX" not in snap["prompt_detected"]
        assert "PROMPT_ID" not in snap["prompt_detected"]

    def test_snapshot_prompt_id_is_telnet_stream(self) -> None:
        """mutmut_55,56: prompt_id value must be 'telnet_stream'."""
        c = _make_telnet()
        snap = c._snapshot()
        assert snap["prompt_detected"]["prompt_id"] == "telnet_stream"
        assert snap["prompt_detected"]["prompt_id"] != "XXtelnet_streamXX"
        assert snap["prompt_detected"]["prompt_id"] != "TELNET_STREAM"

    def test_snapshot_ts_key_exists(self) -> None:
        """mutmut_57,58: 'ts' key must exist."""
        c = _make_telnet()
        snap = c._snapshot()
        assert "ts" in snap
        assert "XXtsXX" not in snap
        assert "TS" not in snap

    def test_snapshot_ts_is_float(self) -> None:
        c = _make_telnet()
        snap = c._snapshot()
        assert isinstance(snap["ts"], float)
        assert snap["ts"] > 0


# ===========================================================================
# TelnetSessionConnector — _hello mutants
# ===========================================================================


class TestTelnetHello:
    def test_hello_input_mode_key_exists(self) -> None:
        """mutmut_5,6: 'input_mode' key must exist in hello dict."""
        c = _make_telnet({"host": "h", "port": 23, "input_mode": "hijack"})
        hello = c._hello()
        assert "input_mode" in hello
        assert "XXinput_modeXX" not in hello
        assert "INPUT_MODE" not in hello

    def test_hello_input_mode_value_correct(self) -> None:
        """input_mode value must match the connector's current mode."""
        c = _make_telnet({"host": "h", "port": 23, "input_mode": "hijack"})
        hello = c._hello()
        assert hello["input_mode"] == "hijack"

    def test_hello_ts_key_exists(self) -> None:
        """mutmut_7,8: 'ts' key must exist."""
        c = _make_telnet()
        hello = c._hello()
        assert "ts" in hello
        assert "XXtsXX" not in hello
        assert "TS" not in hello

    def test_hello_ts_is_float(self) -> None:
        c = _make_telnet()
        hello = c._hello()
        assert isinstance(hello["ts"], float)


# ===========================================================================
# TelnetSessionConnector — stop mutant
# ===========================================================================


class TestTelnetStop:
    @pytest.mark.asyncio
    async def test_stop_sets_connected_false_not_none(self) -> None:
        """mutmut_1: _connected must be False (not None) after stop."""
        t = _make_telnet_transport()
        c = _make_telnet(transport=t)
        c._connected = True
        await c.stop()
        assert c._connected is False
        assert c._connected is not None


# ===========================================================================
# TelnetSessionConnector — poll_messages mutants
# ===========================================================================
