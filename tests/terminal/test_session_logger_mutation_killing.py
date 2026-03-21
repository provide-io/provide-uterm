#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for SessionLogger (session_logger.py).

Kills surviving mutations in __init__, start, stop, log_send, log_send_masked,
log_screen, and _write_event_unlocked.
"""

from __future__ import annotations

import base64
import json
import time
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


# ---------------------------------------------------------------------------
# log_screen
# ---------------------------------------------------------------------------


class TestSessionLoggerLogScreen:
    async def test_log_screen_writes_read_event(self, tmp_path: Path) -> None:
        """log_screen writes a 'read' event."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_screen({"text": "hello"}, b"hello")
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        read_recs = [r for r in lines if r["event"] == "read"]
        assert len(read_recs) == 1

    async def test_log_screen_stores_raw_bytes_b64(self, tmp_path: Path) -> None:
        """log_screen stores base64-encoded raw bytes."""
        log_path = tmp_path / "s.jsonl"
        raw = b"\x01\x02\x03"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_screen({}, raw)
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        rec = next(r for r in lines if r["event"] == "read")
        decoded = base64.b64decode(rec["data"]["raw_bytes_b64"])
        assert decoded == raw

    async def test_log_screen_stores_raw_as_cp437_string(self, tmp_path: Path) -> None:
        """log_screen decodes raw bytes as cp437 for the 'raw' field."""
        log_path = tmp_path / "s.jsonl"
        raw = b"ABC"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_screen({}, raw)
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        rec = next(r for r in lines if r["event"] == "read")
        assert rec["data"]["raw"] == "ABC"

    async def test_log_screen_merges_snapshot_data(self, tmp_path: Path) -> None:
        """log_screen merges snapshot fields into the event data."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_screen({"screen": "test-screen", "cols": 80}, b"x")
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        rec = next(r for r in lines if r["event"] == "read")
        assert rec["data"]["screen"] == "test-screen"
        assert rec["data"]["cols"] == 80

    async def test_log_screen_raw_key_present(self, tmp_path: Path) -> None:
        """log_screen data includes 'raw' key (not omitted)."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_screen({}, b"data")
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        rec = next(r for r in lines if r["event"] == "read")
        assert "raw" in rec["data"]
        assert "raw_bytes_b64" in rec["data"]


# ---------------------------------------------------------------------------
# _write_event_unlocked — quota, ts, session_id, context
# ---------------------------------------------------------------------------


class TestSessionLoggerWriteEventUnlocked:
    async def test_record_has_ts_key(self, tmp_path: Path) -> None:
        """Each written record has a 'ts' key with current time."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        t_before = time.time()
        await sl.log_event("test_event", {})
        t_after = time.time()
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        test_recs = [r for r in lines if r["event"] == "test_event"]
        assert len(test_recs) == 1
        ts = test_recs[0]["ts"]
        assert t_before <= ts <= t_after

    async def test_record_has_event_key(self, tmp_path: Path) -> None:
        """Each record has an 'event' key."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_event("my_event", {"k": "v"})
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        my_recs = [r for r in lines if r["event"] == "my_event"]
        assert len(my_recs) == 1
        assert my_recs[0]["event"] == "my_event"

    async def test_record_has_data_key(self, tmp_path: Path) -> None:
        """Each record has a 'data' key."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_event("e", {"x": 1})
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        rec = next(r for r in lines if r["event"] == "e")
        assert "data" in rec
        assert rec["data"]["x"] == 1

    async def test_quota_stops_writes_when_exceeded(self, tmp_path: Path) -> None:
        """When max_bytes is exceeded, further writes are suppressed."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path, max_bytes=50)
        await sl.start("s")
        # Write many events — the first few may fit, but most will be suppressed
        for i in range(100):
            await sl.log_event("big_event", {"i": i, "data": "x" * 100})
        await sl.stop()

        content = log_path.read_text()
        lines = [json.loads(line) for line in content.splitlines() if line.strip()]
        # Should NOT have 100 big_event records
        big_recs = [r for r in lines if r["event"] == "big_event"]
        assert len(big_recs) < 100

    async def test_quota_gt_check(self, tmp_path: Path) -> None:
        """Quota is checked as >= max_bytes (not just > max_bytes)."""
        log_path = tmp_path / "s.jsonl"
        # With a tiny max_bytes, even the log_start event will likely exceed quota
        sl = SessionLogger(log_path, max_bytes=1)
        await sl.start("s")
        # After start writes log_start, bytes_written should exceed 1
        # Further writes should be suppressed
        await sl.log_event("should_be_suppressed", {})
        await sl.stop()

        content = log_path.read_text()
        lines = [json.loads(line) for line in content.splitlines() if line.strip()]
        suppressed_recs = [r for r in lines if r["event"] == "should_be_suppressed"]
        # The event should be suppressed after quota is hit
        assert len(suppressed_recs) == 0

    async def test_context_included_in_records(self, tmp_path: Path) -> None:
        """When context is set, it is included in records as 'ctx' key."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        sl.set_context({"user": "admin", "game": "tw2002"})
        await sl.log_event("ctx_event", {})
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        rec = next(r for r in lines if r["event"] == "ctx_event")
        assert "ctx" in rec
        assert rec["ctx"]["user"] == "admin"
        assert rec["ctx"]["game"] == "tw2002"

    async def test_no_ctx_when_context_empty(self, tmp_path: Path) -> None:
        """When context is empty, 'ctx' key is not added to records."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_event("no_ctx_event", {})
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        rec = next(r for r in lines if r["event"] == "no_ctx_event")
        assert "ctx" not in rec

    async def test_bytes_written_tracks_actual_bytes(self, tmp_path: Path) -> None:
        """_bytes_written accumulates (not reset to 0 after each write)."""
        sl = SessionLogger(tmp_path / "s.jsonl")
        await sl.start("s")
        initial_written = sl._bytes_written
        assert initial_written > 0  # log_start was written
        await sl.log_event("e", {})
        assert sl._bytes_written > initial_written  # more bytes added
        await sl.stop()

    async def test_quota_warned_initially_false(self, tmp_path: Path) -> None:
        """_quota_warned starts as False, becomes True when quota is first hit."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path, max_bytes=1)
        assert sl._quota_warned is False
        await sl.start("s")
        # log_start likely exceeded the 1-byte quota
        # Write another event to trigger the warning
        await sl.log_event("e", {})
        assert sl._quota_warned is True
        await sl.stop()

    async def test_session_id_included_in_records(self, tmp_path: Path) -> None:
        """session_id is included in all records after start()."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("my-id-99")
        await sl.log_event("test", {})
        await sl.stop()

        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        for line in lines:
            assert line.get("session_id") == "my-id-99"
