#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for SessionLogger."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.recording import RecordingStore
from provide.uterm.session_logger import SessionLogger

if TYPE_CHECKING:
    pass


@pytest.fixture
def mock_store():
    store = MagicMock(spec=RecordingStore)
    store.start_session = AsyncMock()
    store.append_events = AsyncMock()
    store.end_session = AsyncMock()
    store.recording_meta = AsyncMock(return_value={"exists": False, "size_bytes": 0})
    return store


class TestSessionLogger:
    @pytest.mark.asyncio
    async def test_start_stop_writes_header_and_footer(self, mock_store) -> None:
        logger = SessionLogger(mock_store, flush_interval_s=0.1)
        await logger.start(session_id="1")
        await logger.stop()

        mock_store.start_session.assert_called_once_with("1", {"started_at": pytest.approx(time.time(), abs=1)})
        mock_store.end_session.assert_called_once_with("1")

    @pytest.mark.asyncio
    async def test_log_send(self, mock_store) -> None:
        logger = SessionLogger(mock_store, flush_interval_s=0.1)
        await logger.start(session_id="2")
        await logger.log_send("hello")
        await logger.stop()

        # Check append_events was called
        mock_store.append_events.assert_called()
        # Find the 'send' event in any call
        found = False
        for call in mock_store.append_events.call_args_list:
            events = call[0][1]
            for evt in events:
                if evt["event"] == "send" and evt["data"]["keys"] == "hello":
                    found = True
        assert found

    @pytest.mark.asyncio
    async def test_log_send_masked(self, mock_store) -> None:
        logger = SessionLogger(mock_store, flush_interval_s=0.1)
        await logger.start(session_id="3")
        await logger.log_send_masked(byte_count=8)
        await logger.stop()

        found = False
        for call in mock_store.append_events.call_args_list:
            events = call[0][1]
            for evt in events:
                if evt["event"] == "send" and evt["data"].get("masked"):
                    found = True
                    assert evt["data"]["byte_count"] == 8
        assert found

    @pytest.mark.asyncio
    async def test_log_screen_round_trip(self, mock_store) -> None:
        logger = SessionLogger(mock_store, flush_interval_s=0.1)
        await logger.start(session_id="4")
        snapshot = {"prompt_id": "p1"}
        raw = b"screen-data"
        await logger.log_screen(snapshot, raw)
        await logger.stop()

        found = False
        for call in mock_store.append_events.call_args_list:
            events = call[0][1]
            for evt in events:
                if evt["event"] == "read":
                    found = True
                    assert evt["data"]["prompt_id"] == "p1"
                    assert evt["data"]["raw"] == "screen-data"
        assert found

    @pytest.mark.asyncio
    async def test_context_included_in_records(self, mock_store) -> None:
        logger = SessionLogger(mock_store, flush_interval_s=0.1)
        await logger.start(session_id="6")
        logger.set_context({"user": "bob"})
        await logger.log_send("hi")
        await logger.stop()

        found = False
        for call in mock_store.append_events.call_args_list:
            events = call[0][1]
            for evt in events:
                if evt["event"] == "send":
                    found = True
                    assert evt["ctx"] == {"user": "bob"}
        assert found

    @pytest.mark.asyncio
    async def test_quota_writes_stop_at_quota(self, mock_store) -> None:
        # Each event adds 100 to _bytes_written currently
        logger = SessionLogger(mock_store, max_bytes=50, flush_interval_s=0.1)
        await logger.start(session_id="q1")
        await logger.log_event("e1", {})  # written (100)
        await logger.log_event("e2", {})  # suppressed (100 >= 50)
        await logger.stop()

        total_events = 0
        for call in mock_store.append_events.call_args_list:
            total_events += len(call[0][1])
        assert total_events == 1
