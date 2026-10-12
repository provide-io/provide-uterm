#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A receive() cancelled in the tick its read completes keeps the bytes it read.

A hosted session's run loop races ``connector.poll_messages()`` against the
hub's next frame and cancels the poll when the frame wins. If that cancellation
lands in the same event-loop tick as the socket read completing, the bytes have
already left the ``StreamReader``; dropping them lost the telnet banner of
``tests/e2e/telnet/test_sessions.py::test_telnet_screen_shows_received_data``
in roughly one run in eight. The schedule below forces that tick every time.
"""

from __future__ import annotations

import asyncio

import pytest

from provide.uterm.transports.telnet import TelnetTransport


async def _receive_cancelled_as_read_completes(transport: TelnetTransport, reader: asyncio.StreamReader) -> None:
    pending = asyncio.ensure_future(transport.receive(4096, 1000))
    await asyncio.sleep(0)  # receive() is now parked in asyncio.wait on its read task
    reader.feed_data(b"ECHO_BANNER\r\n")
    await asyncio.sleep(0)  # the read task completes; receive() is woken but has not run yet
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


async def test_bytes_read_before_a_cancellation_are_returned_by_the_next_receive() -> None:
    transport = TelnetTransport()
    reader = asyncio.StreamReader()
    transport._reader = reader

    await _receive_cancelled_as_read_completes(transport, reader)

    assert await transport.receive(4096, 50) == b"ECHO_BANNER\r\n"
    # Returned once, not replayed: with nothing new on the socket the next read times out.
    assert await transport.receive(4096, 10) == b""


async def test_a_cancellation_before_any_bytes_arrive_keeps_nothing() -> None:
    transport = TelnetTransport()
    reader = asyncio.StreamReader()
    transport._reader = reader

    pending = asyncio.ensure_future(transport.receive(4096, 1000))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert transport._rx_carry == b""
    reader.feed_data(b"later")
    assert await transport.receive(4096, 50) == b"later"


async def test_a_failed_read_cancelled_in_the_same_tick_keeps_nothing() -> None:
    transport = TelnetTransport()
    reader = asyncio.StreamReader()
    transport._reader = reader

    pending = asyncio.ensure_future(transport.receive(4096, 1000))
    await asyncio.sleep(0)
    reader.set_exception(ConnectionResetError("reset"))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert transport._rx_carry == b""


async def test_disconnect_drops_carried_bytes() -> None:
    transport = TelnetTransport()
    reader = asyncio.StreamReader()
    transport._reader = reader
    await _receive_cancelled_as_read_completes(transport, reader)
    assert transport._rx_carry == b"ECHO_BANNER\r\n"

    class _Writer:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    transport._writer = _Writer()  # type: ignore[assignment]
    await transport.disconnect()

    assert transport._rx_carry == b""
