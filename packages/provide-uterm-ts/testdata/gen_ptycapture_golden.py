#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the PTY capture socket.

Captured terminal traffic arrives framed as ``[1B channel][4B length][payload]``
on a Unix socket, and two of the rules around that framing are the reason this
is worth porting carefully rather than re-deriving:

* **The length cap is checked before the body is read.** A producer claiming a
  four-gigabyte frame is refused *without* the read being attempted — checking
  afterwards would mean allocating what was claimed, which is the whole attack.
  The connection is dropped rather than the frame skipped, because a length
  that large means the stream is no longer trustworthy.
* **The queue drops its oldest frame, not its newest.** A producer faster than
  the reader cannot grow memory without bound, and what a viewer wants when
  something has to go is the most recent screen rather than the stalest.

# uv-package: provide-uterm-platform

Usage (from the repository root)::

    uv run --package provide-uterm-platform python \\
        packages/provide-uterm-ts/testdata/gen_ptycapture_golden.py
"""

from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path
from typing import Any

from provide.uterm.pty import capture

OUT = Path(__file__).resolve().parent / "ptycapture_golden.json"

HEADER = struct.Struct(">BI")


class ScriptedReader:
    """A stream that hands back a fixed buffer, exactly as asked."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    async def readexactly(self, size: int) -> bytes:
        chunk = self._data[self._offset : self._offset + size]
        if len(chunk) < size:
            self._offset = len(self._data)
            raise asyncio.IncompleteReadError(chunk, size)
        self._offset += size
        # Recorded so an over-large claim can be shown never to have been read.
        READS.append(size)
        return chunk


class ScriptedWriter:
    """A writer that only has to be closeable."""

    def __init__(self) -> None:
        self.closes = 0

    def close(self) -> None:
        self.closes += 1

    async def wait_closed(self) -> None:
        return None


READS: list[int] = []


def frame(channel: int, payload: bytes) -> bytes:
    return HEADER.pack(channel, len(payload)) + payload


def claim(channel: int, length: int, payload: bytes = b"") -> bytes:
    """A header claiming *length* bytes, followed by whatever is really there."""
    return HEADER.pack(channel, length) + payload


async def _drive(stream: bytes) -> dict[str, Any]:
    """Read one connection's worth of bytes and record what came out."""
    READS.clear()
    socket = capture.CaptureSocket("/run/uterm/capture.sock")
    writer = ScriptedWriter()
    await socket._handle_connection(ScriptedReader(stream), writer)
    frames = []
    while True:
        got = socket.read_nowait()
        if got is None:
            break
        frames.append({"channel": got.channel, "data": got.data.decode("latin-1")})
    return {"frames": frames, "reads": list(READS), "closes": writer.closes}


STREAMS: list[tuple[str, bytes]] = [
    ("nothing at all", b""),
    ("one frame of output", frame(capture.CHANNEL_STDOUT, b"hello\r\n")),
    ("one frame of input", frame(capture.CHANNEL_STDIN, b"ls\r")),
    ("a connect frame", frame(capture.CHANNEL_CONNECT, b"pts/3")),
    ("two frames", frame(capture.CHANNEL_STDOUT, b"a") + frame(capture.CHANNEL_STDIN, b"b")),
    ("an empty payload", frame(capture.CHANNEL_STDOUT, b"")),
    ("a channel nobody defined", frame(0x7F, b"x")),
    ("high bytes in the payload", frame(capture.CHANNEL_STDOUT, bytes([0xC9, 0xCD, 0xBB]))),
    ("a header that stops short", b"\x01\x00\x00"),
    ("a payload that stops short", claim(capture.CHANNEL_STDOUT, 10, b"abc")),
    ("a frame after one that stops short", claim(capture.CHANNEL_STDOUT, 10, b"abc") + frame(0x01, b"never")),
    ("a frame exactly at the cap", claim(capture.CHANNEL_STDOUT, 16 * 1024 * 1024)),
    ("a frame one byte over the cap", claim(capture.CHANNEL_STDOUT, 16 * 1024 * 1024 + 1)),
    ("a frame claiming four gigabytes", claim(capture.CHANNEL_STDOUT, 0xFFFFFFFF)),
    (
        "a good frame before an over-large one",
        frame(capture.CHANNEL_STDOUT, b"first") + claim(capture.CHANNEL_STDOUT, 0xFFFFFFFF),
    ),
    (
        "a frame after an over-large one",
        claim(capture.CHANNEL_STDOUT, 0xFFFFFFFF) + frame(capture.CHANNEL_STDOUT, b"never"),
    ),
]


async def _backpressure() -> dict[str, Any]:
    """What the queue keeps when a producer outruns its reader."""
    socket = capture.CaptureSocket("/run/uterm/capture.sock")
    maxsize = socket._queue.maxsize
    for index in range(maxsize + 3):
        socket._enqueue(capture.CaptureFrame(channel=capture.CHANNEL_STDOUT, data=str(index).encode()))
    kept = []
    while True:
        got = socket.read_nowait()
        if got is None:
            break
        kept.append(got.data.decode())
    return {
        "maxsize": maxsize,
        "pushed": maxsize + 3,
        "kept": len(kept),
        "first_kept": kept[0],
        "last_kept": kept[-1],
    }


async def main_async() -> None:
    corpus = {
        "channels": {
            "stdout": capture.CHANNEL_STDOUT,
            "stdin": capture.CHANNEL_STDIN,
            "connect": capture.CHANNEL_CONNECT,
        },
        "max_frame_bytes": capture._MAX_FRAME_BYTES,
        "queue_maxsize": capture._QUEUE_MAXSIZE,
        "socket_mode": capture._SOCKET_MODE,
        "bind_umask": capture._BIND_UMASK,
        "header_size": capture._HEADER.size,
        "streams": [
            {"name": name, "stream": stream.decode("latin-1"), **await _drive(stream)} for name, stream in STREAMS
        ],
        "backpressure": await _backpressure(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['streams'])} streams)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
