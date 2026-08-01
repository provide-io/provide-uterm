#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for correlated terminal transcript and screen frames."""

from __future__ import annotations

import asyncio
from typing import Any

from provide.uterm.control_channel import encode_control_frame

from .test_transport_session import _ConcreteSession, _FakeTransport


async def test_wait_for_terminal_frame_correlates_delta_and_snapshot() -> None:
    prompt = b"Enter your choice [T] ?"
    session = _ConcreteSession(_FakeTransport([prompt]), receive_encoding="utf-8")
    since = session.screen_change_seq()

    await session.connect()
    frame = await session.wait_for_terminal_frame(since=since, timeout_ms=1000)
    await session.close()

    assert frame is not None
    assert frame.sequence == since + 1
    assert frame.transcript_delta == prompt.decode()
    assert frame.snapshot["screen"].rstrip().endswith(prompt.decode())
    assert frame.snapshot["cursor"] == frame.cursor


async def test_wait_for_terminal_frame_returns_queued_frames_in_order() -> None:
    session = _ConcreteSession(_FakeTransport([b"first", b" second"]), receive_encoding="utf-8")
    since = session.screen_change_seq()

    await session.connect()
    while session.screen_change_seq() < since + 2:
        await asyncio.sleep(0)

    first = await session.wait_for_terminal_frame(since=since, timeout_ms=1000)
    assert first is not None
    second = await session.wait_for_terminal_frame(since=first.sequence, timeout_ms=1000)
    await session.close()

    assert second is not None
    assert first.sequence == since + 1
    assert first.transcript_delta == "first"
    assert first.snapshot["screen"].rstrip().endswith("first")
    assert second.sequence == since + 2
    assert second.transcript_delta == " second"
    assert second.snapshot["screen"].rstrip().endswith("first second")


async def test_wait_for_terminal_frame_timeout_returns_none_within_budget() -> None:
    session = _ConcreteSession(_FakeTransport())
    loop = asyncio.get_running_loop()
    started = loop.time()

    frame = await session.wait_for_terminal_frame(since=0, timeout_ms=20)

    elapsed = loop.time() - started
    assert frame is None
    assert elapsed < 0.2


async def test_terminal_frame_excludes_control_protocol_bytes_from_delta() -> None:
    control = encode_control_frame({"type": "render_speed", "cps": 2400}).encode()
    session = _ConcreteSession(
        _FakeTransport([control + b"terminal text"]),
        receive_encoding="utf-8",
        control_frames=True,
    )

    await session.connect()
    frame = await session.wait_for_terminal_frame(since=0, timeout_ms=1000)
    await session.close()

    assert frame is not None
    assert frame.transcript_delta == "terminal text"
    assert "render_speed" not in frame.transcript_delta


class _MutableSnapshotEmulator:
    def __init__(self) -> None:
        self.current: dict[str, Any] = {
            "screen": "",
            "cursor": {"x": 0, "y": 0},
            "metadata": {"nested": {"state": "original"}},
        }

    def process(self, data: bytes) -> None:
        self.current["screen"] = data.decode()
        self.current["cursor"]["x"] = len(data)

    def get_snapshot(self) -> dict[str, Any]:
        return self.current


async def test_terminal_frame_owns_nested_snapshot_and_copies_cursor() -> None:
    session = _ConcreteSession(_FakeTransport([b"owned"]), receive_encoding="utf-8")
    emulator = _MutableSnapshotEmulator()
    session._emulator = emulator  # type: ignore[assignment]

    await session.connect()
    frame = await session.wait_for_terminal_frame(since=0, timeout_ms=1000)
    await session.close()

    assert frame is not None
    emulator.current["cursor"]["x"] = 999
    emulator.current["metadata"]["nested"]["state"] = "mutated"
    copied_cursor = frame.cursor
    copied_cursor["x"] = 123

    assert frame.snapshot["cursor"] == {"x": 5, "y": 0}
    assert frame.snapshot["metadata"] == {"nested": {"state": "original"}}
    assert frame.cursor == {"x": 5, "y": 0}
