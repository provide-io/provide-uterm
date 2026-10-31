#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""How a hosted session runtime ends one connection's recording, never raising.

Self-contained (a bare mixin host, mocked recording calls), so the mutation
gate can bind runtime_connection.py without the runtime's own fragile suites.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from provide.uterm.server import runtime
from provide.uterm.server.config_schema_session import SessionDefinition
from provide.uterm.server.runtime_connection import ConnectionRecordingMixin


class _Host(ConnectionRecordingMixin):
    def __init__(self, *, stopping: bool = False) -> None:
        self.definition = SessionDefinition(session_id="s1")
        self._stop = asyncio.Event()
        if stopping:
            self._stop.set()
        self._last_error: str | None = None
        self._pending_close_failure: Exception | None = None
        self.stop_mock = AsyncMock()
        self.log_mock = AsyncMock()
        self._stop_recording = self.stop_mock  # type: ignore[method-assign]
        self._log_event = self.log_mock  # type: ignore[method-assign]


async def test_a_clean_close_records_nothing() -> None:
    host = _Host()
    await host._end_connection_recording("dropped")
    host.stop_mock.assert_awaited_once_with()
    assert (host._last_error, host._pending_close_failure) == (None, None)


async def test_a_failed_close_is_logged_and_kept_after_the_connection_error() -> None:
    host = _Host()
    failure = OSError("disk full")
    host.stop_mock.side_effect = failure
    with patch.object(runtime, "logger") as logger:
        await host._end_connection_recording("connection reset")
    assert host._last_error == "connection reset; recording close failed: disk full"
    # Not stopping: a close failure on a reconnect is raised nowhere.
    assert host._pending_close_failure is None
    logger.warning.assert_called_once_with(
        "hosted_session_recording_close_failed session_id=%s error=%s", "s1", failure
    )


async def test_a_failed_close_after_a_clean_end_is_the_close_error_alone() -> None:
    host = _Host()
    host.stop_mock.side_effect = OSError("disk full")
    await host._end_connection_recording()
    assert host._last_error == "recording close failed: disk full"


async def test_a_failed_close_while_stopping_is_kept_for_stop_to_raise() -> None:
    host = _Host(stopping=True)
    failure = OSError("disk full")
    host.stop_mock.side_effect = failure
    await host._end_connection_recording(None)
    assert host._pending_close_failure is failure


async def test_a_runtime_error_is_written_to_the_recording() -> None:
    host = _Host()
    payload: dict[str, Any] = {"error": "boom"}
    await host._log_runtime_error(payload)
    host.log_mock.assert_awaited_once_with("runtime_error", payload)


async def test_a_runtime_error_that_cannot_be_written_is_logged_not_raised() -> None:
    host = _Host()
    failure = OSError("store down")
    host.log_mock.side_effect = failure
    with patch.object(runtime, "logger") as logger:
        await host._log_runtime_error({"error": "boom"})
    logger.warning.assert_called_once_with(
        "hosted_session_runtime_error_log_failed session_id=%s error=%s", "s1", failure
    )
