#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Transport-agnostic guarded send helpers."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Protocol

from provide.uterm.sanitizer import prepare_keystrokes


class ExpectSession(Protocol):
    async def send(self, data: str) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...

    def screen_change_seq(self) -> int: ...

    async def wait_for_screen_change(self, *, timeout_ms: int = 5000, since: int | None = None) -> bool: ...


@dataclass(frozen=True)
class ExpectResult:
    """Result from :func:`send_and_expect`."""

    matched: bool
    matched_text: str | None
    screen: str
    timed_out: bool


def _find_match(screen: str, expect_text: str | None, expect_re: re.Pattern[str] | None) -> str | None:
    if expect_text is not None and expect_text in screen:
        return expect_text
    if expect_re is not None:
        match = expect_re.search(screen)
        if match is not None:
            return str(match.group(0))
    return None


async def send_and_expect(
    session: ExpectSession,
    keys: str,
    *,
    expect_text: str | None = None,
    expect_regex: str | None = None,
    timeout_ms: int = 5000,
    sanitize: bool = True,
) -> ExpectResult:
    """Send keys and wait until the expected text or regex appears."""
    payload = prepare_keystrokes(keys) if sanitize else keys
    expect_re = re.compile(expect_regex) if expect_regex is not None else None
    since = session.screen_change_seq()
    # An empty payload is a no-op write; skip it so callers can use this as a
    # pure read/wait (e.g. a read-only snapshot) without emitting a stray frame.
    if payload:
        await session.send(payload)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, timeout_ms) / 1000.0
    last_screen = str(session.snapshot().get("screen", ""))
    matched = _find_match(last_screen, expect_text, expect_re)
    if matched is not None:
        return ExpectResult(True, matched, last_screen, False)

    if expect_text is None and expect_re is None:
        remaining = max(0, int((deadline - loop.time()) * 1000))
        await session.wait_for_screen_change(timeout_ms=remaining, since=since)
        screen = str(session.snapshot().get("screen", ""))
        return ExpectResult(False, None, screen, False)

    while True:
        remaining_s = deadline - loop.time()
        if remaining_s <= 0:
            return ExpectResult(False, None, last_screen, True)
        changed = await session.wait_for_screen_change(timeout_ms=max(1, int(remaining_s * 1000)), since=since)
        last_screen = str(session.snapshot().get("screen", ""))
        matched = _find_match(last_screen, expect_text, expect_re)
        if matched is not None:
            return ExpectResult(True, matched, last_screen, False)
        if not changed:
            return ExpectResult(False, None, last_screen, True)
        since = session.screen_change_seq()
