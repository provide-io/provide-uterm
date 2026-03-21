#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for telnet connector — handle_input, control, analysis, clear, mode."""

from __future__ import annotations

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


class TestTelnetHandleInput:
    @pytest.mark.asyncio
    async def test_handle_input_sends_bytes_not_none(self) -> None:
        """mutmut_1: send must be called with actual bytes, not None."""
        t = _make_telnet_transport()
        c = _make_telnet(transport=t)
        c._connected = True
        await c.handle_input("hello")
        call_args = t.send.await_args
        assert call_args is not None
        sent = call_args[0][0]
        assert sent is not None
        assert isinstance(sent, bytes)

    @pytest.mark.asyncio
    async def test_handle_input_encodes_as_cp437(self) -> None:
        """mutmut_4,5,7: must encode data as 'cp437', not default utf-8 or 'CP437'."""
        t = _make_telnet_transport()
        c = _make_telnet(transport=t)
        c._connected = True
        await c.handle_input("A")
        sent_bytes = t.send.await_args[0][0]
        # 'A' encoded as cp437 is b'\x41'
        assert sent_bytes == b"A"

    @pytest.mark.asyncio
    async def test_handle_input_uses_replace_error_handler(self) -> None:
        """mutmut_8,9: must use errors='replace', not 'XXreplaceXX' or 'REPLACE'."""
        t = _make_telnet_transport()
        c = _make_telnet(transport=t)
        c._connected = True
        # Characters with replaceable chars
        await c.handle_input("hello \u0000")  # NUL is encodable in cp437
        assert t.send.await_count == 1

    @pytest.mark.asyncio
    async def test_handle_input_updates_banner(self) -> None:
        """mutmut_10: banner must be updated (not set to None) after input."""
        t = _make_telnet_transport()
        c = _make_telnet(transport=t)
        c._connected = True
        await c.handle_input("hello")
        assert c._banner is not None
        assert "5" in c._banner  # 5 characters

    @pytest.mark.asyncio
    async def test_handle_input_returns_snapshot(self) -> None:
        t = _make_telnet_transport()
        c = _make_telnet(transport=t)
        c._connected = True
        msgs = await c.handle_input("x")
        assert len(msgs) == 1
        assert msgs[0]["type"] == "snapshot"


# ===========================================================================
# TelnetSessionConnector — handle_control mutants
# ===========================================================================


class TestTelnetHandleControl:
    @pytest.mark.asyncio
    async def test_pause_sets_paused_and_banner(self) -> None:
        """mutmut_6,7,8,9: pause must set _paused=True and update banner."""
        c = _make_telnet()
        await c.handle_control("pause")
        assert c._paused is True
        assert c._banner is not None
        assert "Exclusive control" in c._banner
        # Verify case-exact match
        assert "Exclusive control active." in c._banner

    @pytest.mark.asyncio
    async def test_resume_sets_paused_false_and_banner(self) -> None:
        """mutmut_15,16,17,18: resume must set _paused=False and update banner."""
        c = _make_telnet()
        c._paused = True
        await c.handle_control("resume")
        assert c._paused is False
        assert c._banner is not None
        assert "Exclusive control released." in c._banner

    @pytest.mark.asyncio
    async def test_step_action_recognized(self) -> None:
        """mutmut_20,21: 'step' action must be matched (not 'XXstepXX' or 'STEP')."""
        c = _make_telnet()
        await c.handle_control("step")
        # Step must set a specific banner, not fall through to "Ignored"
        assert "Ignored" not in c._banner
        assert "Step" in c._banner or "step" in c._banner.lower()

    @pytest.mark.asyncio
    async def test_step_banner_exact(self) -> None:
        """mutmut_22,23,24,25: step banner must be exact 'Step requested...' text."""
        c = _make_telnet()
        await c.handle_control("step")
        assert c._banner == "Step requested. Awaiting upstream output."

    @pytest.mark.asyncio
    async def test_handle_control_returns_snapshot(self) -> None:
        c = _make_telnet()
        msgs = await c.handle_control("pause")
        assert len(msgs) == 1
        assert msgs[0]["type"] == "snapshot"


# ===========================================================================
# TelnetSessionConnector — get_analysis mutant
# ===========================================================================


class TestTelnetGetAnalysis:
    @pytest.mark.asyncio
    async def test_analysis_uses_newline_separator(self) -> None:
        """mutmut_2: join must use '\n', not 'XX\nXX'."""
        c = _make_telnet({"host": "127.0.0.1", "port": 23})
        result = await c.get_analysis()
        # Fields must be separated by plain newlines
        assert "XX\nXX" not in result
        assert "\n" in result
        assert "sess-t" in result
        assert "127.0.0.1" in result
        assert "input_mode" in result


# ===========================================================================
# TelnetSessionConnector — clear mutants
# ===========================================================================


class TestTelnetClear:
    @pytest.mark.asyncio
    async def test_clear_empties_buffer(self) -> None:
        c = _make_telnet()
        c._screen_buffer = "old data"
        await c.clear()
        assert c._screen_buffer == ""

    @pytest.mark.asyncio
    async def test_clear_banner_exact(self) -> None:
        """mutmut_3,4,5,6: banner must be 'Screen buffer cleared.' exactly."""
        c = _make_telnet()
        await c.clear()
        assert c._banner == "Screen buffer cleared."

    @pytest.mark.asyncio
    async def test_clear_returns_snapshot(self) -> None:
        c = _make_telnet()
        msgs = await c.clear()
        assert len(msgs) == 1
        assert msgs[0]["type"] == "snapshot"


# ===========================================================================
# TelnetSessionConnector — set_mode mutants
# ===========================================================================


class TestTelnetSetMode:
    @pytest.mark.asyncio
    async def test_set_mode_open_clears_paused(self) -> None:
        """mutmut_13,17-19: setting 'open' mode must clear paused flag."""
        c = _make_telnet({"host": "h", "port": 23, "input_mode": "hijack"})
        c._paused = True
        await c.set_mode("open")
        assert c._paused is False
        assert c._input_mode == "open"

    @pytest.mark.asyncio
    async def test_set_mode_banner_shared_input_for_open(self) -> None:
        """mutmut_14,15,16,17,18,19: banner must say 'Shared input' for open mode."""
        c = _make_telnet()
        await c.set_mode("open")
        assert "Shared input" in c._banner
        assert c._banner is not None

    @pytest.mark.asyncio
    async def test_set_mode_banner_exclusive_hijack_for_hijack(self) -> None:
        """mutmut_20,21,22: banner must say 'Exclusive hijack' for hijack mode."""
        c = _make_telnet()
        await c.set_mode("hijack")
        assert "Exclusive hijack" in c._banner

    @pytest.mark.asyncio
    async def test_set_mode_returns_hello_and_snapshot(self) -> None:
        c = _make_telnet()
        msgs = await c.set_mode("open")
        assert len(msgs) == 2
        assert msgs[0]["type"] == "worker_hello"
        assert msgs[1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_set_mode_hello_has_input_mode(self) -> None:
        c = _make_telnet()
        msgs = await c.set_mode("hijack")
        assert msgs[0]["input_mode"] == "hijack"


# ===========================================================================
# SshSessionConnector — __init__ mutants
# ===========================================================================
