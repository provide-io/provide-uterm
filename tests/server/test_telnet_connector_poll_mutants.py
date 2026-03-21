#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for telnet connector — poll messages, screen, hello."""

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


class TestTelnetPollMessages:
    @pytest.mark.asyncio
    async def test_poll_calls_receive_with_correct_args(self) -> None:
        """mutmut_3,4,5,6,7,8: receive must be called with (4096, 100)."""
        t = _make_telnet_transport(connected=True, recv_data=b"hello")
        c = _make_telnet(transport=t)
        c._connected = True
        await c.poll_messages()
        t.receive.assert_awaited_once_with(4096, 100)

    @pytest.mark.asyncio
    async def test_poll_accumulates_received_bytes(self) -> None:
        """mutmut_10: _received_bytes must be incremented (+=), not assigned (=)."""
        t = _make_telnet_transport(connected=True, recv_data=b"hello")
        c = _make_telnet(transport=t)
        c._connected = True
        c._received_bytes = 5  # pre-set to 5
        await c.poll_messages()
        assert c._received_bytes == 10  # 5 + 5

    @pytest.mark.asyncio
    async def test_poll_screen_buffer_capped_at_32000(self) -> None:
        """mutmut_16,17: screen buffer must be capped at 32000 chars, not more."""
        t = _make_telnet_transport(connected=True, recv_data=b"x" * 20000)
        c = _make_telnet(transport=t)
        c._connected = True
        c._screen_buffer = "y" * 20000
        await c.poll_messages()
        assert len(c._screen_buffer) <= 32000

    @pytest.mark.asyncio
    async def test_poll_screen_buffer_not_empty_sliced(self) -> None:
        """mutmut_16: buffer must use [-32000:] not [+32000:] (would give empty)."""
        t = _make_telnet_transport(connected=True, recv_data=b"hello")
        c = _make_telnet(transport=t)
        c._connected = True
        c._screen_buffer = ""
        await c.poll_messages()
        # After receiving "hello", buffer must contain the text
        assert len(c._screen_buffer) > 0

    @pytest.mark.asyncio
    async def test_poll_updates_banner_with_byte_count(self) -> None:
        """mutmut_18: banner must be updated with received byte count, not None."""
        t = _make_telnet_transport(connected=True, recv_data=b"hello")
        c = _make_telnet(transport=t)
        c._connected = True
        await c.poll_messages()
        assert c._banner is not None
        assert "5" in c._banner
        assert "bytes" in c._banner.lower() or "received" in c._banner.lower()

    @pytest.mark.asyncio
    async def test_poll_returns_data_key_in_term_message(self) -> None:
        """mutmut_23,24: term message must have key 'data', not 'XXdataXX' or 'DATA'."""
        t = _make_telnet_transport(connected=True, recv_data=b"hi")
        c = _make_telnet(transport=t)
        c._connected = True
        msgs = await c.poll_messages()
        term_msgs = [m for m in msgs if m.get("type") == "term"]
        assert len(term_msgs) == 1
        assert "data" in term_msgs[0]
        assert "XXdataXX" not in term_msgs[0]
        assert "DATA" not in term_msgs[0]

    @pytest.mark.asyncio
    async def test_poll_returns_ts_key_in_term_message(self) -> None:
        """mutmut_25,26: term message must have key 'ts', not 'XXtsXX' or 'TS'."""
        t = _make_telnet_transport(connected=True, recv_data=b"hi")
        c = _make_telnet(transport=t)
        c._connected = True
        msgs = await c.poll_messages()
        term_msgs = [m for m in msgs if m.get("type") == "term"]
        assert len(term_msgs) == 1
        assert "ts" in term_msgs[0]
        assert "XXtsXX" not in term_msgs[0]
        assert "TS" not in term_msgs[0]
        assert isinstance(term_msgs[0]["ts"], float)


# ===========================================================================
# TelnetSessionConnector — handle_input mutants
# ===========================================================================
