#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import hashlib
from typing import Any

import pytest
from provide.uterm.expect import send_and_expect


class _ScriptedSession:
    def __init__(self, screens: list[str]) -> None:
        self._screens = screens
        self._idx = 0
        self.sent: list[str] = []
        self.seq = 0

    def snapshot(self) -> dict[str, Any]:
        screen = self._screens[min(self._idx, len(self._screens) - 1)]
        return {"screen": screen, "screen_hash": hashlib.sha256(screen.encode()).hexdigest()}

    def screen_change_seq(self) -> int:
        return self.seq

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def wait_for_screen_change(self, *, timeout_ms: int, since: int | None = None) -> bool:
        del timeout_ms
        if self._idx + 1 >= len(self._screens):
            return False
        self._idx += 1
        self.seq += 1
        return since is None or self.seq > since


@pytest.mark.asyncio
async def test_send_and_expect_matches_text_after_update() -> None:
    session = _ScriptedSession(["Loading", "Command [TL=00:00]:"])
    result = await send_and_expect(session, "D\r", expect_text="Command [", timeout_ms=1000)

    assert result.matched is True
    assert result.matched_text == "Command ["
    assert result.timed_out is False
    assert result.screen == "Command [TL=00:00]:"
    assert session.sent == ["D\r"]


@pytest.mark.asyncio
async def test_send_and_expect_matches_initial_snapshot_after_send() -> None:
    session = _ScriptedSession(["Command [TL=00:00]:"])
    result = await send_and_expect(session, "D\r", expect_text="Command [", timeout_ms=1000)

    assert result.matched is True
    assert result.matched_text == "Command ["
    assert result.timed_out is False
    assert session.sent == ["D\r"]


@pytest.mark.asyncio
async def test_send_and_expect_zero_timeout_returns_deadline_timeout() -> None:
    session = _ScriptedSession(["Loading", "Command [TL=00:00]:"])
    result = await send_and_expect(session, "D\r", expect_text="Command [", timeout_ms=0)

    assert result.matched is False
    assert result.timed_out is True
    assert result.screen == "Loading"


@pytest.mark.asyncio
async def test_send_and_expect_times_out_without_match() -> None:
    session = _ScriptedSession(["Loading", "Still loading"])
    result = await send_and_expect(session, "D\r", expect_text="Command [", timeout_ms=1000)

    assert result.matched is False
    assert result.matched_text is None
    assert result.timed_out is True
    assert result.screen == "Still loading"


@pytest.mark.asyncio
async def test_send_and_expect_matches_regex() -> None:
    session = _ScriptedSession(["Loading", "Sector 42 : Credits: 15,000"])
    result = await send_and_expect(session, "I\r", expect_regex=r"Sector\s+(\d+)", timeout_ms=1000)

    assert result.matched is True
    assert result.matched_text == "Sector 42"
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_send_and_expect_sanitizes_by_default() -> None:
    session = _ScriptedSession(["Ready", "Ready"])
    await send_and_expect(session, "hello\x00\r", timeout_ms=1000)

    assert session.sent == ["hello\r"]


@pytest.mark.asyncio
async def test_send_and_expect_without_expectation_settles_after_first_update() -> None:
    session = _ScriptedSession(["Before", "After", "Later"])
    result = await send_and_expect(session, "x", timeout_ms=1000, sanitize=False)

    assert result.matched is False
    assert result.timed_out is False
    assert result.screen == "After"
    assert session.sent == ["x"]


@pytest.mark.asyncio
async def test_send_and_expect_empty_keys_does_not_send() -> None:
    """An empty payload must not be written to the wire — this lets callers use
    send_and_expect as a pure read/wait (e.g. a read-only snapshot tool)."""
    session = _ScriptedSession(["Command [TL=00:00]:"])
    result = await send_and_expect(session, "", timeout_ms=0)

    assert session.sent == []
    assert result.matched is False
    assert result.timed_out is False
