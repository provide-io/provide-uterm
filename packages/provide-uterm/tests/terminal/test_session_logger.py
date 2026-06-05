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

    @pytest.mark.asyncio
    async def test_log_send_applies_redaction(self, mock_store) -> None:
        logger = SessionLogger(mock_store, redactor=lambda text: text.replace("password=secret", "password=[REDACTED]"))
        await logger.start(session_id="red-send")
        await logger.log_send("login password=secret")
        await logger.stop()

        found = False
        for call in mock_store.append_events.call_args_list:
            events = call[0][1]
            for evt in events:
                if evt["event"] == "send":
                    found = True
                    assert evt["data"]["keys"] == "login password=[REDACTED]"
                    assert "secret" not in evt["data"]["keys"]
        assert found

    @pytest.mark.asyncio
    async def test_log_screen_applies_redaction_to_snapshot_and_raw(self, mock_store) -> None:
        logger = SessionLogger(mock_store, redactor=lambda text: text.replace("token=abc123", "token=[REDACTED]"))
        await logger.start(session_id="red-read")
        await logger.log_screen({"screen": "token=abc123"}, b"token=abc123")
        await logger.stop()

        found = False
        for call in mock_store.append_events.call_args_list:
            events = call[0][1]
            for evt in events:
                if evt["event"] == "read":
                    found = True
                    assert evt["data"]["screen"] == "token=[REDACTED]"
                    assert evt["data"]["raw"] == "token=[REDACTED]"
                    assert "abc123" not in evt["data"]["raw"]
        assert found

    @pytest.mark.asyncio
    async def test_log_screen_applies_redaction_to_nested_lists(self, mock_store) -> None:
        logger = SessionLogger(mock_store, redactor=lambda text: text.replace("secret", "[REDACTED]"))
        await logger.start(session_id="red-list")
        await logger.log_screen({"tokens": ["secret", {"nested": ["keep", "secret"]}]}, b"")
        await logger.stop()

        found = False
        for call in mock_store.append_events.call_args_list:
            events = call[0][1]
            for evt in events:
                if evt["event"] == "read":
                    found = True
                    assert evt["data"]["tokens"] == ["[REDACTED]", {"nested": ["keep", "[REDACTED]"]}]
        assert found

    @pytest.mark.asyncio
    async def test_log_wire_applies_redaction(self, mock_store) -> None:
        logger = SessionLogger(
            mock_store,
            control_channel_mode="wire",
            redactor=lambda text: text.replace("Authorization: Bearer SECRET", "Authorization: Bearer [REDACTED]"),
        )
        await logger.start(session_id="red-wire")
        await logger.log_wire("send", "Authorization: Bearer SECRET")
        await logger.stop()

        found = False
        for call in mock_store.append_events.call_args_list:
            events = call[0][1]
            for evt in events:
                if evt["event"] == "wire_send":
                    found = True
                    assert evt["data"]["text"] == "Authorization: Bearer [REDACTED]"
                    assert "SECRET" not in evt["data"]["text"]
        assert found

    @pytest.mark.asyncio
    async def test_log_send_masked_still_writes_mask_placeholder(self, mock_store) -> None:
        logger = SessionLogger(mock_store, redactor=lambda text: text.replace("***", "changed"))
        await logger.start(session_id="masked")
        await logger.log_send_masked(byte_count=9)
        await logger.stop()

        found = False
        for call in mock_store.append_events.call_args_list:
            events = call[0][1]
            for evt in events:
                if evt["event"] == "send" and evt["data"].get("masked"):
                    found = True
                    assert evt["data"]["keys"] == "***"
                    assert evt["data"]["byte_count"] == 9
        assert found

    @pytest.mark.asyncio
    async def test_flush_retains_batch_when_store_raises(self, mock_store) -> None:
        """A failing append_events must NOT drop the buffered batch.

        The buffer is only cleared after a successful append, so a transient
        store failure leaves the events buffered for the next flush attempt.
        """
        # First append fails; subsequent appends succeed.
        mock_store.append_events = AsyncMock(side_effect=[RuntimeError("store down"), None])

        logger = SessionLogger(mock_store, flush_interval_s=1000.0)
        # Drive _periodic_flush out of the picture: never start its task, so
        # only our explicit flush() calls touch the buffer.
        logger._session_id = "retry"
        await logger.log_send("important")

        # First flush hits the failing store and must propagate the error.
        with pytest.raises(RuntimeError, match="store down"):
            await logger.flush()

        # The batch is still buffered — not lost.
        assert len(logger._buffer) == 1
        assert logger._buffer[0]["event"] == "send"

        # A subsequent successful flush delivers the events exactly once.
        await logger.flush()
        assert logger._buffer == []
        assert mock_store.append_events.call_count == 2
        delivered = mock_store.append_events.call_args_list[1][0][1]
        assert [e["event"] for e in delivered] == ["send"]

    @pytest.mark.asyncio
    async def test_flush_clears_buffer_on_success(self, mock_store) -> None:
        """The happy path empties the buffer after a successful append."""
        logger = SessionLogger(mock_store, flush_interval_s=1000.0)
        logger._session_id = "ok"
        await logger.log_send("a")
        await logger.log_send("b")
        assert len(logger._buffer) == 2

        await logger.flush()
        assert logger._buffer == []
        mock_store.append_events.assert_called_once()
        delivered = mock_store.append_events.call_args[0][1]
        assert len(delivered) == 2
