#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for server connectors — SSH connector."""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


class TestSshInit:
    def test_display_name_stored_not_none(self) -> None:
        """mutmut_48: _display_name must be set, not None."""
        c = _make_ssh()
        assert c._display_name == "Test SSH"
        assert c._display_name is not None

    def test_host_from_config(self) -> None:
        """mutmut_49,51,52,54,55,56: host must be read from config key 'host'."""
        pytest.importorskip("asyncssh")
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        c = SshSessionConnector("s", "S", {"host": "192.0.2.99", "insecure_no_host_check": True})
        assert c._host == "192.0.2.99"

    def test_host_defaults_to_telnet_host(self) -> None:
        """mutmut_52: missing host must fall back to TerminalDefaults.TELNET_HOST."""
        from provide.terminal.defaults import TerminalDefaults

        c = _make_ssh({"insecure_no_host_check": True})
        assert c._host == TerminalDefaults.TELNET_HOST

    def test_username_from_config(self) -> None:
        """mutmut_66-72: username must be read from config key 'username'."""
        pytest.importorskip("asyncssh")
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        c = SshSessionConnector("s", "S", {"host": "h", "insecure_no_host_check": True, "username": "admin"})
        assert c._username == "admin"

    def test_username_defaults_to_guest(self) -> None:
        """mutmut_68,73,74: missing username must fall back to 'guest'."""
        c = _make_ssh()
        assert c._username == "guest"

    def test_password_none_when_not_in_config(self) -> None:
        """mutmut_75,79: _password must be None when not provided in config."""
        c = _make_ssh()
        assert c._password is None

    def test_password_stored_from_config(self) -> None:
        """mutmut_75,76,77,78,79,80,81,82,83: password must be stored from config."""
        pytest.importorskip("asyncssh")
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        c = SshSessionConnector("s", "S", {"host": "h", "insecure_no_host_check": True, "password": "secret99"})
        assert c._password == "secret99"

    def test_password_is_none_not_set_to_str_none(self) -> None:
        """mutmut_79: when password absent, _password must be None (not str of something)."""
        c = _make_ssh()
        assert c._password is None

    def test_input_mode_from_config(self) -> None:
        """mutmut_114-123: input_mode must be read from config key 'input_mode'."""
        pytest.importorskip("asyncssh")
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        c = SshSessionConnector("s", "S", {"host": "h", "insecure_no_host_check": True, "input_mode": "hijack"})
        assert c._input_mode == "hijack"

    def test_input_mode_defaults_to_open(self) -> None:
        """mutmut_117,119,122,123: default input_mode must be 'open'."""
        c = _make_ssh()
        assert c._input_mode == "open"

    def test_paused_initially_false_not_none(self) -> None:
        """mutmut_124: _paused must start False, not None."""
        c = _make_ssh()
        assert c._paused is False
        assert c._paused is not None

    def test_paused_initially_false_not_true(self) -> None:
        """mutmut_125: _paused must start False, not True."""
        c = _make_ssh()
        assert c._paused is False

    def test_connected_initially_false_not_none(self) -> None:
        """mutmut_126: _connected must start False, not None."""
        c = _make_ssh()
        assert c._connected is False
        assert c._connected is not None

    def test_connected_initially_false_not_true(self) -> None:
        """mutmut_127: _connected must start False, not True."""
        c = _make_ssh()
        assert c._connected is False
        assert not c.is_connected()

    def test_banner_not_none_and_includes_host(self) -> None:
        """mutmut_130: _banner must be a string containing host info, not None."""
        pytest.importorskip("asyncssh")
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        c = SshSessionConnector("s", "S", {"host": "myhost.example", "insecure_no_host_check": True})
        assert c._banner is not None
        assert "myhost.example" in c._banner

    def test_no_known_hosts_without_insecure_raises(self) -> None:
        """mutmut_100-103: ValueError must be raised if no known_hosts and no insecure flag."""
        pytest.importorskip("asyncssh")
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        with pytest.raises(ValueError, match="known_hosts"):
            SshSessionConnector("s", "S", {"host": "h"})

    def test_insecure_no_host_check_skips_error(self) -> None:
        """insecure_no_host_check=True must suppress the known_hosts error."""
        c = _make_ssh({"host": "h", "insecure_no_host_check": True})
        assert c._known_hosts is None

    def test_known_hosts_stored(self) -> None:
        pytest.importorskip("asyncssh")
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        c = SshSessionConnector("s", "S", {"host": "h", "known_hosts": "/etc/known_hosts"})
        assert c._known_hosts == "/etc/known_hosts"


# ===========================================================================
# SshSessionConnector — _screen mutants
# ===========================================================================


class TestSshScreen:
    def test_separator_is_dashes(self) -> None:
        """mutmut_3: separator must be '-'*60."""
        c = _make_ssh()
        screen = c._screen()
        assert "-" * 60 in screen

    def test_separator_length_60(self) -> None:
        """mutmut_4: separator must be 60 dashes, not 61."""
        c = _make_ssh()
        screen = c._screen()
        assert "-" * 60 in screen
        # verify 61 consecutive dashes don't appear
        assert "-" * 61 not in screen

    def test_open_mode_shows_shared_input(self) -> None:
        """mutmut_5,6,7: 'Shared input' must appear in open mode."""
        pytest.importorskip("asyncssh")
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        c = SshSessionConnector("s", "S", {"host": "h", "insecure_no_host_check": True, "input_mode": "open"})
        screen = c._screen()
        assert "Shared input" in screen

    def test_hijack_mode_shows_exclusive_hijack(self) -> None:
        """mutmut_8-13: condition/string must discriminate open vs hijack."""
        pytest.importorskip("asyncssh")
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        c_open = SshSessionConnector("s", "S", {"host": "h", "insecure_no_host_check": True, "input_mode": "open"})
        c_hijack = SshSessionConnector("s", "S", {"host": "h", "insecure_no_host_check": True, "input_mode": "hijack"})
        assert "Shared input" in c_open._screen()
        assert "Exclusive hijack" in c_hijack._screen()

    def test_paused_shows_paused_for_hijack(self) -> None:
        """mutmut_14: 'Paused for hijack' must appear when _paused."""
        c = _make_ssh()
        c._paused = True
        assert "Paused for hijack" in c._screen()

    def test_live_shows_when_not_paused(self) -> None:
        """mutmut_17: 'Live' must appear when not paused."""
        c = _make_ssh()
        c._paused = False
        assert "Live" in c._screen()

    def test_no_xxxx_in_screen(self) -> None:
        """mutmut_20,22: no 'XXXX' or 'XX\nXX' in screen output."""
        c = _make_ssh()
        screen = c._screen()
        assert "XXXX" not in screen
        assert "XX\nXX" not in screen

    def test_screen_joined_with_plain_newline(self) -> None:
        """mutmut_22: lines must be joined with '\n'."""
        c = _make_ssh()
        screen = c._screen()
        assert "\n" in screen
        assert "XX\nXX" not in screen


# ===========================================================================
# SshSessionConnector — _snapshot mutants
# ===========================================================================


class TestSshSnapshot:
    def test_snapshot_type(self) -> None:
        c = _make_ssh()
        snap = c._snapshot()
        assert snap["type"] == "snapshot"

    def test_snapshot_cursor_x_bounded(self) -> None:
        """mutmut_6,7,22,23: cursor x must use lines[-1] and be bounded by _COLS-1."""
        c = _make_ssh()
        c._screen_buffer = "A" * 200
        snap = c._snapshot()
        assert snap["cursor"]["x"] == 79

    def test_snapshot_cursor_keys(self) -> None:
        """mutmut_14-17,24,25: cursor must have keys 'x' and 'y', not variants."""
        c = _make_ssh()
        snap = c._snapshot()
        assert "cursor" in snap
        assert "x" in snap["cursor"]
        assert "y" in snap["cursor"]

    def test_snapshot_cursor_y_bounded(self) -> None:
        """mutmut_30-33: cursor y must be bounded by _ROWS-1 (24)."""
        c = _make_ssh()
        c._screen_buffer = "\n".join(["line"] * 100)
        snap = c._snapshot()
        assert snap["cursor"]["y"] <= 24

    def test_snapshot_cols_80(self) -> None:
        c = _make_ssh()
        assert c._snapshot()["cols"] == 80

    def test_snapshot_rows_25(self) -> None:
        """mutmut_34-37: rows must be 25."""
        c = _make_ssh()
        snap = c._snapshot()
        assert snap["rows"] == 25
        assert "rows" in snap

    def test_snapshot_screen_hash_16_chars(self) -> None:
        """mutmut_43,44: screen_hash must be 16 chars."""
        c = _make_ssh()
        snap = c._snapshot()
        assert len(snap["screen_hash"]) == 16

    def test_snapshot_screen_hash_matches(self) -> None:
        """mutmut_38,39,43: screen_hash must be sha256 of screen, utf-8 encoded."""
        c = _make_ssh()
        snap = c._snapshot()
        expected = hashlib.sha256(snap["screen"].encode("utf-8")).hexdigest()[:16]
        assert snap["screen_hash"] == expected

    def test_snapshot_cursor_at_end_is_true(self) -> None:
        """mutmut_45-47: cursor_at_end must be True."""
        c = _make_ssh()
        snap = c._snapshot()
        assert snap["cursor_at_end"] is True

    def test_snapshot_has_trailing_space_is_false(self) -> None:
        """mutmut_48-50: has_trailing_space must be False."""
        c = _make_ssh()
        snap = c._snapshot()
        assert snap["has_trailing_space"] is False

    def test_snapshot_prompt_id_is_ssh_stream(self) -> None:
        """mutmut_51-58: prompt_id must be 'ssh_stream'."""
        c = _make_ssh()
        snap = c._snapshot()
        assert snap["prompt_detected"]["prompt_id"] == "ssh_stream"

    def test_snapshot_ts_key_exists_and_is_float(self) -> None:
        """mutmut_57,58: 'ts' key must exist."""
        c = _make_ssh()
        snap = c._snapshot()
        assert "ts" in snap
        assert isinstance(snap["ts"], float)


# ===========================================================================
# SshSessionConnector — _hello mutants
# ===========================================================================


class TestSshHello:
    def test_hello_input_mode_key(self) -> None:
        """mutmut_5,6: 'input_mode' key must exist."""
        pytest.importorskip("asyncssh")
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        c = SshSessionConnector("s", "S", {"host": "h", "insecure_no_host_check": True, "input_mode": "hijack"})
        hello = c._hello()
        assert "input_mode" in hello
        assert hello["input_mode"] == "hijack"

    def test_hello_ts_key(self) -> None:
        """mutmut_7,8: 'ts' key must exist."""
        c = _make_ssh()
        hello = c._hello()
        assert "ts" in hello
        assert isinstance(hello["ts"], float)


# ===========================================================================
# SshSessionConnector — start mutants
# ===========================================================================


class TestSshStart:
    @pytest.mark.asyncio
    async def test_start_passes_host_to_connect(self) -> None:
        """mutmut_2: connect must be called with self._host, not None."""
        pytest.importorskip("asyncssh")
        import asyncssh

        from provide.terminal.server.connectors.ssh import SshSessionConnector

        c = SshSessionConnector("s", "S", {"host": "myhost.example", "insecure_no_host_check": True})
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_conn = MagicMock()
        mock_conn.create_process = AsyncMock(return_value=mock_process)
        with patch.object(asyncssh, "connect", new=AsyncMock(return_value=mock_conn)) as mock_connect:
            await c.start()
            call_args = mock_connect.call_args
            # First positional arg must be the host
            assert call_args[0][0] == "myhost.example"

    @pytest.mark.asyncio
    async def test_start_wires_stdin_stdout_connected(self) -> None:
        """mutmut_5,6,7,10,12,13,16,17,21-29: start must wire process fields correctly."""
        pytest.importorskip("asyncssh")
        import asyncssh

        c = _make_ssh()
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_conn = MagicMock()
        mock_conn.create_process = AsyncMock(return_value=mock_process)
        with patch.object(asyncssh, "connect", new=AsyncMock(return_value=mock_conn)):
            await c.start()
        assert c._connected is True
        assert c._stdin is mock_process.stdin
        assert c._stdout is mock_process.stdout
        assert c._conn is mock_conn
        assert c._process is mock_process

    @pytest.mark.asyncio
    async def test_start_create_process_uses_ansi_term(self) -> None:
        """mutmut_21,22,23,24,25,26,27: create_process must use term_type='ansi'."""
        pytest.importorskip("asyncssh")
        import asyncssh

        c = _make_ssh()
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_conn = MagicMock()
        mock_conn.create_process = AsyncMock(return_value=mock_process)
        with patch.object(asyncssh, "connect", new=AsyncMock(return_value=mock_conn)):
            await c.start()
        call_kwargs = mock_conn.create_process.await_args[1]
        assert call_kwargs.get("term_type") == "ansi"
        assert call_kwargs.get("term_size") == (80, 25)

    @pytest.mark.asyncio
    async def test_start_process_not_none(self) -> None:
        """mutmut_29: _process must not be None after start."""
        pytest.importorskip("asyncssh")
        import asyncssh

        c = _make_ssh()
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_conn = MagicMock()
        mock_conn.create_process = AsyncMock(return_value=mock_process)
        with patch.object(asyncssh, "connect", new=AsyncMock(return_value=mock_conn)):
            await c.start()
        assert c._process is not None
        assert c._process is mock_process


# ===========================================================================
# SshSessionConnector — stop mutants
# ===========================================================================


class TestSshStop:
    @pytest.mark.asyncio
    async def test_stop_process_set_to_none(self) -> None:
        """mutmut_4: _process must be None (not '') after stop."""
        c = _make_ssh()
        _, _, _ = _attach_ssh(c)
        c._process = MagicMock()
        c._process.close = MagicMock()
        await c.stop()
        assert c._process is None

    @pytest.mark.asyncio
    async def test_stop_stdout_set_to_none(self) -> None:
        """mutmut_7: _stdout must be None (not '') after stop."""
        c = _make_ssh()
        _attach_ssh(c)
        await c.stop()
        assert c._stdout is None

    @pytest.mark.asyncio
    async def test_stop_connected_is_false_not_none(self) -> None:
        """mutmut_8: _connected must be False (not None) after stop."""
        c = _make_ssh()
        _attach_ssh(c)
        await c.stop()
        assert c._connected is False
        assert c._connected is not None

    @pytest.mark.asyncio
    async def test_stop_suppress_uses_exception_not_none(self) -> None:
        """mutmut_11,13,15: contextlib.suppress must use Exception, not None."""
        c = _make_ssh()
        mock_conn = MagicMock()
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()
        mock_process = MagicMock()
        mock_process.close = MagicMock(side_effect=RuntimeError("oops"))
        mock_stdin = MagicMock()
        mock_stdin.write_eof = MagicMock(side_effect=RuntimeError("oops"))
        c._conn = mock_conn
        c._process = mock_process
        c._stdin = mock_stdin
        c._stdout = MagicMock()
        c._connected = True
        # Must not raise; errors must be suppressed
        await c.stop()
        assert c._connected is False


# ===========================================================================
# SshSessionConnector — poll_messages mutants
# ===========================================================================
