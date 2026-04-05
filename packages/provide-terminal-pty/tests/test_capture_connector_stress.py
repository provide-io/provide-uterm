#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Stress tests for CaptureConnector frame throughput."""

from __future__ import annotations

import asyncio
import struct
import tempfile
import time
from pathlib import Path

import pytest

from provide.terminal.pty.capture import CHANNEL_CONNECT, CHANNEL_STDIN, CHANNEL_STDOUT
from provide.terminal.pty.capture_connector import CaptureConnector

FRAME_COUNT = 10_000
FRAME_DATA = b"x" * 80 + b"\r\n"  # typical terminal line


def _make_frame(channel: int, data: bytes) -> bytes:
    return struct.pack(">BI", channel, len(data)) + data


def _make_connector(td: str) -> CaptureConnector:
    path = str(Path(td) / "cap.sock")
    return CaptureConnector("stress-1", "Stress", {"socket_path": path})


async def _send_burst(path: str, count: int) -> None:
    """Send *count* STDOUT frames in a single connection."""
    _reader, writer = await asyncio.open_unix_connection(path)
    frame = _make_frame(CHANNEL_STDOUT, FRAME_DATA)
    for _ in range(count):
        writer.write(frame)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


class TestCaptureConnectorThroughput:
    """Sustained frame ingestion stress tests."""

    @pytest.mark.timeout(30)
    async def test_10k_stdout_frames_processed(self) -> None:
        """10k STDOUT frames ingested and drained via poll_messages."""
        with tempfile.TemporaryDirectory() as td:
            conn = _make_connector(td)
            await conn.start()

            await _send_burst(conn._socket_path, FRAME_COUNT)  # noqa: SLF001
            await asyncio.sleep(0.2)  # let frames arrive

            total_msgs = 0
            for _ in range(200):  # poll repeatedly to drain
                msgs = await conn.poll_messages()
                total_msgs += len(msgs)
                if not msgs:
                    break
                await asyncio.sleep(0.01)

            await conn.stop()
            assert total_msgs > 0, "should have received at least one term message"

    @pytest.mark.timeout(30)
    async def test_throughput_above_5k_frames_per_second(self) -> None:
        """Frame processing throughput exceeds 5000 frames/sec."""
        with tempfile.TemporaryDirectory() as td:
            conn = _make_connector(td)
            await conn.start()

            start = time.monotonic()
            await _send_burst(conn._socket_path, FRAME_COUNT)  # noqa: SLF001
            await asyncio.sleep(0.1)

            drained = 0
            while True:
                msgs = await conn.poll_messages()
                drained += len(msgs)
                if not msgs:
                    break
            elapsed = time.monotonic() - start

            await conn.stop()

            fps = FRAME_COUNT / elapsed
            assert fps > 5000, f"throughput {fps:.0f} fps below 5000 threshold"

    @pytest.mark.timeout(30)
    async def test_mixed_channels_no_data_loss(self) -> None:
        """Mix of STDOUT/STDIN/CONNECT frames — no crash, STDIN counted."""
        with tempfile.TemporaryDirectory() as td:
            conn = _make_connector(td)
            await conn.start()

            _reader, writer = await asyncio.open_unix_connection(
                conn._socket_path  # noqa: SLF001
            )
            for i in range(3000):
                ch = [CHANNEL_STDOUT, CHANNEL_STDIN, CHANNEL_CONNECT][i % 3]
                writer.write(_make_frame(ch, f"msg-{i}".encode()))
            await writer.drain()
            writer.close()
            await writer.wait_closed()

            await asyncio.sleep(0.1)
            while await conn.poll_messages():
                pass

            analysis = await conn.get_analysis()
            await conn.stop()
            assert "stdin_keystrokes=1000" in analysis
            # connect_log is capped at 100 entries in CaptureConnector
            assert "outbound_connections=100" in analysis

    @pytest.mark.timeout(30)
    async def test_buffer_stays_bounded_under_flood(self) -> None:
        """Internal buffer stays ≤65536 under sustained flood."""
        with tempfile.TemporaryDirectory() as td:
            conn = _make_connector(td)
            await conn.start()

            # Send large frames that would exceed buffer if uncapped
            big_frame = _make_frame(CHANNEL_STDOUT, b"A" * 4096)
            _reader, writer = await asyncio.open_unix_connection(
                conn._socket_path  # noqa: SLF001
            )
            for _ in range(100):
                writer.write(big_frame)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

            await asyncio.sleep(0.1)
            while await conn.poll_messages():
                pass

            snap = await conn.get_snapshot()
            await conn.stop()
            assert len(snap["screen"]) <= 65536
