#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Reader ingest counters must be readable from outside the session.

The session counts every chunk and every byte it takes off the socket, but
those numbers never left the process — so a consumer watching a frozen screen
could not tell "no bytes ever arrived here" from "bytes arrived and the
emulator never reflected them". Those two need opposite fixes and look
identical from the screen tail alone, which is exactly how a diagnostic ended
up reporting ``-1`` at the one moment it mattered.
"""

from __future__ import annotations

import asyncio
from typing import Any

from provide.uterm.transport_session import TransportSession


class _FakeTransport:
    """Scripted transport: ``receive`` replays chunks, then idles."""

    def __init__(self, script: list[bytes] | None = None) -> None:
        self.script = list(script or [])
        self._idx = 0

    async def connect(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send(self, data: bytes) -> None:
        return None

    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        if self._idx < len(self.script):
            item = self.script[self._idx]
            self._idx += 1
            return item
        await asyncio.sleep(0.01)
        return b""

    def is_connected(self) -> bool:
        return True


class _Session(TransportSession):
    """Concrete session — the base leaves ``_connect_transport`` abstract."""

    async def _connect_transport(self) -> None:
        await self._transport.connect("h", 1, cols=self._cols, rows=self._rows)


def test_counts_start_at_zero() -> None:
    """A session that has read nothing reports zero, never a sentinel."""
    session = _Session(_FakeTransport())

    assert session.reader_ingest_counts() == (0, 0)


async def test_counts_advance_per_chunk_and_byte() -> None:
    """Each chunk off the wire bumps chunks by one and bytes by its length."""
    session = _Session(_FakeTransport([b"abc", b"de"]))
    await session.connect()
    try:
        for _ in range(200):
            if session.reader_ingest_counts()[0] >= 2:
                break
            await asyncio.sleep(0.005)
    finally:
        await session.close()

    chunks, nbytes = session.reader_ingest_counts()
    assert chunks == 2, f"expected one count per chunk, got {chunks}"
    assert nbytes == 5, f"expected the summed chunk lengths, got {nbytes}"


async def test_empty_reads_do_not_count() -> None:
    """An idle wire returning b'' is not ingest and must not inflate the counts."""
    session = _Session(_FakeTransport())
    await session.connect()
    try:
        await asyncio.sleep(0.05)  # several idle receive() rounds
    finally:
        await session.close()

    assert session.reader_ingest_counts() == (0, 0)


def test_counts_track_the_reader_fields() -> None:
    """The accessor reports the reader's own counters, not a parallel tally.

    A separate tally would drift from the loop it is meant to describe, which
    is the failure mode this accessor exists to avoid.
    """
    session = _Session(_FakeTransport())
    session._change_seq = 7
    session._bytes_total = 4096

    assert session.reader_ingest_counts() == (7, 4096)
