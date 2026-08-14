#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The reader loop's own logging must never be able to kill the reader loop.

``_reader_loop`` calls ``logger.trace()``. That method exists on
provide.telemetry's wrapper and NOT on ``logging.getLogger()``'s return value,
which is what this module used to hold. The resulting ``AttributeError`` is not
in ``_reader_loop``'s ``except`` tuple, so the loop died on its FIRST chunk and
``finally`` set ``_connected = False`` — a silent death with no traceback
anywhere, because the loop runs as a background task nobody awaits.

Live 2026-08-14 against TWGS: the reader consumed the leading NUL, died, and so
never answered the ``IAC DO 246`` that arrived in the next packet. Negotiation
never completed, TWGS sent nothing further, and every parity bot login timed out
against a blank screen while the transport underneath was working perfectly.

One byte in, one chunk processed, then silence — from a log line.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from provide.uterm.transport_session import TransportSession

from provide.uterm import transport_session as ts_mod


def test_the_module_logger_supports_the_level_the_reader_loop_calls() -> None:
    """A stdlib logger here is the bug: it has no ``trace`` and the loop calls it."""
    assert hasattr(ts_mod.logger, "trace"), (
        f"{type(ts_mod.logger)!r} has no .trace(); _reader_loop calls it and "
        "AttributeError is not caught there — the reader dies on chunk 1"
    )


class _OneChunkThenIdle:
    """Transport that yields one chunk, then nothing — the shape that broke."""

    def __init__(self) -> None:
        self.receives = 0

    async def connect(self, *a: Any, **kw: Any) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send(self, data: bytes) -> None:
        return None

    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        self.receives += 1
        if self.receives == 1:
            return b"\x00"
        await asyncio.sleep(0.01)
        return b""

    def is_connected(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_the_reader_keeps_reading_after_the_first_chunk() -> None:
    """The regression itself: chunk 1 must not be the last thing the loop does.

    Asserted on *subsequent reads* rather than on bytes, because the failure
    mode was not "wrong data" — it was the loop never asking for more.
    """

    class _Session(TransportSession):
        async def _connect_transport(self) -> None:
            await self._transport.connect()

    transport = _OneChunkThenIdle()
    session = _Session(transport, cols=80, rows=25)
    await session.connect()
    try:
        await asyncio.sleep(0.2)
        assert transport.receives > 1, (
            f"reader stopped after {transport.receives} read(s) — it died inside the loop body instead of continuing"
        )
        assert session._connected, "reader loop exited and marked the session disconnected"
    finally:
        await session.close()
