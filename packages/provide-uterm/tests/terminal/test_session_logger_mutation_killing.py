#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for SessionLogger (session_logger.py) — part 1.

Classes: TestSessionLoggerInit, TestSessionLoggerStart, TestSessionLoggerStop,
         TestSessionLoggerLogSend, TestSessionLoggerLogSendMasked.

Part 2 (TestSessionLoggerLogScreen, TestSessionLoggerWriteEventUnlocked)
is in test_session_logger_mutation_killing_2.py.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from provide.terminal.session_logger import SessionLogger

# ---------------------------------------------------------------------------
# __init__ — stored attributes
# ---------------------------------------------------------------------------


class TestSessionLoggerInit:
    def test_log_path_converted_to_path(self, tmp_path: Path) -> None:
        """_log_path is a Path object, not a string."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(str(log_path))
        assert isinstance(sl._log_path, Path)

    def test_log_path_stored_as_path(self, tmp_path: Path) -> None:
        """_log_path stores the Path value (not None)."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        assert sl._log_path == log_path

    def test_session_id_starts_as_none(self, tmp_path: Path) -> None:
        """_session_id starts as None, not '' (kills mutmut_6)."""
        sl = SessionLogger(tmp_path / "s.jsonl")
        assert sl._session_id is None

    def test_context_starts_as_empty_dict(self, tmp_path: Path) -> None:
        """_context starts as {} (not None) (kills mutmut_7)."""
        sl = SessionLogger(tmp_path / "s.jsonl")
        assert sl._context == {}
        assert sl._context is not None

    def test_max_bytes_default_is_0(self, tmp_path: Path) -> None:
        """Default max_bytes is 0 (kills mutmut_1 which sets it to 1)."""
        sl = SessionLogger(tmp_path / "s.jsonl")
        assert sl._max_bytes == 0

    def test_max_bytes_custom_value_stored(self, tmp_path: Path) -> None:
        """Custom max_bytes is stored correctly."""
        sl = SessionLogger(tmp_path / "s.jsonl", max_bytes=1000)
        assert sl._max_bytes == 1000

    def test_bytes_written_starts_at_0(self, tmp_path: Path) -> None:
        """_bytes_written starts at 0 (not 1)."""
        sl = SessionLogger(tmp_path / "s.jsonl")
        assert sl._bytes_written == 0

    def test_quota_warned_starts_false(self, tmp_path: Path) -> None:
        """_quota_warned starts as False (not None, not True)."""
        sl = SessionLogger(tmp_path / "s.jsonl")
        assert sl._quota_warned is False


# ---------------------------------------------------------------------------
# start — session_id, file mode, mkdir, event type
# ---------------------------------------------------------------------------


class TestSessionLoggerStart:
    async def test_session_id_set_after_start(self, tmp_path: Path) -> None:
        """After start(), _session_id equals the passed id (kills mutmut_16)."""
        sl = SessionLogger(tmp_path / "s.jsonl")
        await sl.start("my-session-42")
        try:
            assert sl._session_id == "my-session-42"
        finally:
            await sl.stop()

    async def test_log_start_event_written(self, tmp_path: Path) -> None:
        """start() writes a 'log_start' event (kills mutmut_17/18)."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("sess1")
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert lines[0]["event"] == "log_start"

    async def test_log_start_has_path_key(self, tmp_path: Path) -> None:
        """log_start event data has a 'path' key (kills mutmut_18 — data=None)."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert "path" in lines[0]["data"]

    async def test_log_start_has_started_at_key(self, tmp_path: Path) -> None:
        """log_start data has 'started_at' key."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert "started_at" in lines[0]["data"]

    async def test_start_creates_parent_dirs(self, tmp_path: Path) -> None:
        """start() creates parent directories (parents=True)."""
        deep_path = tmp_path / "a" / "b" / "c" / "s.jsonl"
        sl = SessionLogger(deep_path)
        await sl.start("sess")
        await sl.stop()
        assert deep_path.exists()

    async def test_start_can_reopen_existing_file(self, tmp_path: Path) -> None:
        """start() opens in append mode (not read mode or write/truncate)."""
        log_path = tmp_path / "s.jsonl"
        # Create file with existing content
        log_path.write_text("existing\n")

        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.stop()

        content = log_path.read_text()
        # Append mode: original content preserved
        assert "existing" in content

    async def test_session_id_in_written_records(self, tmp_path: Path) -> None:
        """Records written after start() include session_id."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("test-session")
        await sl.log_event("test_evt", {})
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        test_evt = next(r for r in lines if r["event"] == "test_evt")
        assert test_evt["session_id"] == "test-session"


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


class TestSessionLoggerStop:
    async def test_stop_writes_log_stop_event(self, tmp_path: Path) -> None:
        """stop() writes a 'log_stop' event."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert lines[-1]["event"] == "log_stop"

    async def test_stop_sets_file_to_none(self, tmp_path: Path) -> None:
        """After stop(), _file is None."""
        sl = SessionLogger(tmp_path / "s.jsonl")
        await sl.start("s")
        await sl.stop()
        assert sl._file is None


# ---------------------------------------------------------------------------
# log_send — encoding, keys, event type
# ---------------------------------------------------------------------------


class TestSessionLoggerLogSend:
    async def test_log_send_writes_send_event(self, tmp_path: Path) -> None:
        """log_send writes an event with type 'send'."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_send("hello")
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        send_recs = [r for r in lines if r["event"] == "send"]
        assert len(send_recs) == 1

    async def test_log_send_stores_keys(self, tmp_path: Path) -> None:
        """log_send stores the keys string in data['keys']."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_send("test-keys")
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        send_rec = next(r for r in lines if r["event"] == "send")
        assert send_rec["data"]["keys"] == "test-keys"

    async def test_log_send_stores_bytes_b64(self, tmp_path: Path) -> None:
        """log_send stores base64-encoded bytes in data['bytes_b64']."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_send("abc")
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        send_rec = next(r for r in lines if r["event"] == "send")
        # bytes_b64 must be valid base64
        decoded = base64.b64decode(send_rec["data"]["bytes_b64"])
        assert isinstance(decoded, bytes)

    async def test_log_send_cp437_encoding(self, tmp_path: Path) -> None:
        """log_send encodes keys as cp437 bytes."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_send("A")
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        send_rec = next(r for r in lines if r["event"] == "send")
        decoded = base64.b64decode(send_rec["data"]["bytes_b64"])
        # 'A' in cp437 is 0x41
        assert decoded == b"A"

    async def test_log_send_no_masked_field_for_normal_keys(self, tmp_path: Path) -> None:
        """Normal log_send does not include 'masked' key in data."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_send("visible")
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        send_rec = next(r for r in lines if r["event"] == "send")
        assert "masked" not in send_rec["data"]


# ---------------------------------------------------------------------------
# log_send_masked
# ---------------------------------------------------------------------------


class TestSessionLoggerLogSendMasked:
    async def test_log_send_masked_masked_field_true(self, tmp_path: Path) -> None:
        """log_send_masked sets masked=True."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_send_masked(8)
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        rec = next(r for r in lines if r["event"] == "send")
        assert rec["data"]["masked"] is True

    async def test_log_send_masked_keys_is_stars(self, tmp_path: Path) -> None:
        """log_send_masked stores '***' as keys."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_send_masked(5)
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        rec = next(r for r in lines if r["event"] == "send")
        assert rec["data"]["keys"] == "***"

    async def test_log_send_masked_byte_count_stored(self, tmp_path: Path) -> None:
        """log_send_masked stores byte_count in data."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_send_masked(byte_count=12)
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        rec = next(r for r in lines if r["event"] == "send")
        assert rec["data"]["byte_count"] == 12

    async def test_log_send_masked_bytes_b64_is_stars_encoded(self, tmp_path: Path) -> None:
        """log_send_masked stores base64 of b'***' in bytes_b64."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_send_masked(3)
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        rec = next(r for r in lines if r["event"] == "send")
        decoded = base64.b64decode(rec["data"]["bytes_b64"])
        assert decoded == b"***"


# (TestSessionLoggerLogScreen and TestSessionLoggerWriteEventUnlocked
#  moved to test_session_logger_mutation_killing_2.py)
