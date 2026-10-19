#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A multibyte character split across capture frames still arrives whole.

The shim sends each write() the captured program makes as its own frame, and a
program that flushes a buffer mid-character splits a UTF-8 sequence across two
frames. Decoding every frame on its own turned each half into U+FFFD.
"""

import asyncio
import struct
import tempfile
from pathlib import Path

import pytest

from provide.uterm import strip_ansi
from provide.uterm.pty.capture import CHANNEL_STDOUT
from provide.uterm.pty.capture_connector import CaptureConnector


def _connector(td: str = "/tmp") -> CaptureConnector:
    return CaptureConnector("utf8", "UTF-8", {"socket_path": str(Path(td) / "cap.sock")})


def test_a_character_split_across_two_chunks_is_decoded_whole() -> None:
    conn = _connector()
    euro = "€".encode()

    assert conn._ingest_stdout(euro[:1]) == ""
    assert conn._ingest_stdout(euro[1:]) == "€"
    assert conn._buffer == "€"
    assert strip_ansi(conn._snapshot()["screen"]).startswith("€")


@pytest.mark.parametrize("cut", [1, 2, 3])
def test_every_cut_through_a_four_byte_character_is_decoded_whole(cut: int) -> None:
    conn = _connector()
    data = "a😀b".encode()

    text = conn._ingest_stdout(data[: cut + 1]) + conn._ingest_stdout(data[cut + 1 :])

    assert text == "a😀b"
    assert conn._since_clear == "a😀b"


def test_a_character_split_across_three_chunks_is_decoded_whole() -> None:
    conn = _connector()
    euro = "€".encode()

    assert [conn._ingest_stdout(euro[i : i + 1]) for i in range(3)] == ["", "", "€"]


def test_invalid_bytes_are_still_replaced() -> None:
    conn = _connector()

    assert conn._ingest_stdout(b"a\xffb") == "a\ufffdb"


def test_a_sequence_the_next_chunk_does_not_complete_is_replaced() -> None:
    conn = _connector()

    assert conn._ingest_stdout(b"\xe2\x82") == ""
    assert conn._ingest_stdout(b"x") == "\ufffdx"


def test_each_session_decodes_its_own_stream() -> None:
    first, second = _connector(), _connector()
    euro = "€".encode()

    first._ingest_stdout(euro[:2])

    assert second._ingest_stdout(b"z") == "z"
    assert first._ingest_stdout(euro[2:]) == "€"


async def test_a_split_character_reaches_the_viewer_whole_over_the_socket() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _connector(td)
        await conn.start()
        euro = "€".encode()
        _reader, writer = await asyncio.open_unix_connection(conn._socket_path)
        writer.write(struct.pack(">BI", CHANNEL_STDOUT, 1) + euro[:1])
        await writer.drain()
        await asyncio.sleep(0.05)
        # The first half alone draws nothing, so there is nothing to stream yet.
        assert await conn.poll_messages() == []
        writer.write(struct.pack(">BI", CHANNEL_STDOUT, 2) + euro[1:])
        await writer.drain()
        await asyncio.sleep(0.05)
        assert await conn.poll_messages() == [{"type": "term", "data": "€"}]
        writer.close()
        await writer.wait_closed()
        await conn.stop()
