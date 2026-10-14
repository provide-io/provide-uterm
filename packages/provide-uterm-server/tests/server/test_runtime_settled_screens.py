#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A recording can keep the screen of a session nobody is reading.

A connector that emits raw output (a PAM capture session) records a screen only
when one is sent, and one is sent only when a viewer asks: a session nobody
watched held its opening blank screen and nothing else, and replay showed that.
recording.screen_interval_s records the screen as it changes instead.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.defaults import TerminalDefaults
from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime

from ..helpers import decode_chunk  # noqa: TID252

SETTLE = TerminalDefaults.RECORDED_SCREEN_SETTLE_S
INTERVAL = 1.0


def _runtime(interval: float = INTERVAL) -> HostedSessionRuntime:
    rt = HostedSessionRuntime(
        SessionDefinition(
            session_id="settle",
            display_name="Settle",
            connector_type="pty_capture",
            auto_start=False,
        ),
        public_base_url="http://localhost:9999",
        recording=RecordingConfig(screen_interval_s=interval),
    )
    rt._logger = MagicMock()
    rt._logger.log_screen = AsyncMock()
    rt._logger.log_event = AsyncMock()
    rt._logger.log_wire = AsyncMock()
    rt._logger.log_control = AsyncMock()
    return rt


def _connector(screens: list[str]) -> MagicMock:
    connector = AsyncMock()
    connector.get_snapshot = AsyncMock(side_effect=[{"type": "snapshot", "screen": screen} for screen in screens])
    return connector


def _recorded(rt: HostedSessionRuntime) -> list[str]:
    return [call.args[0]["screen"] for call in rt._logger.log_screen.await_args_list]


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def test_a_burst_of_output_is_recorded_once_it_settles() -> None:
    rt = _runtime()
    connector = _connector(["painted"])
    clock = _Clock()
    with patch("provide.uterm.server.runtime.time.monotonic", clock):
        rt._note_output()
        await rt._record_settled_screen(connector)
        clock.now += SETTLE / 2
        rt._note_output()
        await rt._record_settled_screen(connector)
        assert _recorded(rt) == []
        clock.now += SETTLE
        await rt._record_settled_screen(connector)
        await rt._record_settled_screen(connector)
    assert _recorded(rt) == ["painted"]


async def test_output_that_never_stops_is_recorded_once_an_interval() -> None:
    rt = _runtime()
    connector = _connector(["first", "second"])
    clock = _Clock()
    with patch("provide.uterm.server.runtime.time.monotonic", clock):
        for _ in range(10):
            rt._note_output()
            await rt._record_settled_screen(connector)
            clock.now += SETTLE
    # A chunk every 0.25 s for 2.5 s: a screen 1 s in, and another 1 s after
    # the first chunk that screen did not hold.
    assert _recorded(rt) == ["first", "second"]


async def test_screens_are_never_closer_than_the_interval() -> None:
    rt = _runtime()
    connector = _connector(["one", "two"])
    clock = _Clock()
    with patch("provide.uterm.server.runtime.time.monotonic", clock):
        rt._note_output()
        clock.now += SETTLE
        await rt._record_settled_screen(connector)
        # A short burst that settles well inside the interval waits for it.
        rt._note_output()
        clock.now += SETTLE
        await rt._record_settled_screen(connector)
        assert _recorded(rt) == ["one"]
        clock.now += INTERVAL
        await rt._record_settled_screen(connector)
    assert _recorded(rt) == ["one", "two"]


async def test_a_screen_like_the_last_one_recorded_is_not_recorded_again() -> None:
    rt = _runtime()
    connector = _connector(["same", "same"])
    clock = _Clock()
    with patch("provide.uterm.server.runtime.time.monotonic", clock):
        for _ in range(2):
            rt._note_output()
            clock.now += INTERVAL
            await rt._record_settled_screen(connector)
    assert _recorded(rt) == ["same"]
    assert connector.get_snapshot.await_count == 2


async def test_a_screen_a_viewer_read_counts_as_recorded() -> None:
    rt = _runtime()
    connector = _connector(["unwanted"])
    clock = _Clock()
    with patch("provide.uterm.server.runtime.time.monotonic", clock):
        rt._note_output()
        await rt._log_snapshot({"type": "snapshot", "screen": "read by a viewer"})
        clock.now += INTERVAL * 2
        await rt._record_settled_screen(connector)
    assert _recorded(rt) == ["read by a viewer"]
    connector.get_snapshot.assert_not_awaited()


@pytest.mark.parametrize("recorded", [False, True])
async def test_nothing_is_taken_unless_asked_for_and_recorded(recorded: bool) -> None:
    # Off by default; and asked for but not recorded is off too.
    rt = _runtime(interval=0.0 if recorded else INTERVAL)
    if not recorded:
        rt._logger = None
    connector = _connector(["unwanted"])
    clock = _Clock()
    with patch("provide.uterm.server.runtime.time.monotonic", clock):
        rt._note_output()
        clock.now += INTERVAL * 2
        await rt._record_settled_screen(connector)
    connector.get_snapshot.assert_not_awaited()


def test_the_interval_is_off_by_default_and_never_negative() -> None:
    assert RecordingConfig().screen_interval_s == 0.0
    with pytest.raises(ValueError, match="screen_interval_s"):
        RecordingConfig(screen_interval_s=-1)


async def test_an_unread_capture_session_records_what_it_painted() -> None:
    rt = _runtime(interval=SETTLE)
    rt._queue = asyncio.Queue()
    rt._stop = asyncio.Event()
    output = [[{"type": "term", "data": "hello"}]]

    async def poll() -> list[dict[str, Any]]:
        await asyncio.sleep(0.01)
        return output.pop(0) if output else []

    connector = AsyncMock()
    connector.set_mode = AsyncMock(return_value=[])
    connector.get_snapshot = AsyncMock(
        side_effect=[{"type": "snapshot", "screen": ""}, {"type": "snapshot", "screen": "hello"}]
    )
    connector.poll_messages = poll
    rt._connector = connector

    class _Ws:
        sent: list[dict[str, Any]] = []

        async def send(self, data: str) -> None:
            self.sent.extend(decode_chunk(data, data_type="term"))

        async def recv(self) -> str:
            await asyncio.sleep(SETTLE * 4)
            rt._stop.set()
            await asyncio.sleep(100)
            return ""

    await rt._bridge_session(_Ws())
    # The opening screen, then the painted one, though no viewer asked.
    assert _recorded(rt) == ["", "hello"]
