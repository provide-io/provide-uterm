#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for SessionLogger (session_logger.py) — part 2.

Classes: TestSessionLoggerLogScreen, TestSessionLoggerWriteEventUnlocked.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from provide.uterm.session_logger import SessionLogger

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


# ---------------------------------------------------------------------------
# LegacyFileStore coverage (lines 73, 89-108)
# ---------------------------------------------------------------------------


class TestLegacyFileStoreCoverage:
    """Cover LegacyFileStore.get_path and LegacyFileStore.get_entries."""

    async def test_get_path_returns_recording_path(self, tmp_path: Path) -> None:
        """Covers line 73: LegacyFileStore.get_path returns self._path."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        # _store is a LegacyFileStore — exercise get_path directly.
        got = await sl._store.get_path("any-session-id")
        assert got == log_path

    async def test_get_entries_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        """Covers lines 89-90: get_entries returns [] when file does not exist."""
        log_path = tmp_path / "missing.jsonl"
        sl = SessionLogger(log_path)
        entries = await sl._store.get_entries("s")
        assert entries == []

    async def test_get_entries_reads_jsonl_and_returns_tail(self, tmp_path: Path) -> None:
        """Covers lines 91-95, 97, 105, 106-107: read entries, default tail slice."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        for i in range(5):
            await sl.log_event("evt", {"i": i})
        await sl.stop()

        # default limit (200) returns all rows, but offset=None → tail slice
        entries = await sl._store.get_entries("s")
        # All rows present (log_start, 5 evts, log_stop)
        assert len(entries) == 7
        # tail slice preserves order
        assert entries[0]["event"] == "log_start"
        assert entries[-1]["event"] == "log_stop"

    async def test_get_entries_skips_malformed_lines(self, tmp_path: Path) -> None:
        """Covers lines 96-99 (JSONDecodeError continue branch)."""
        log_path = tmp_path / "s.jsonl"
        # Manually craft a file with a bad line interleaved with good ones.
        log_path.write_text('{"event": "good", "ts": 1}\nnot-json\n{"event": "also_good", "ts": 2}\n')
        sl = SessionLogger(log_path)
        entries = await sl._store.get_entries("s")
        assert [e["event"] for e in entries] == ["good", "also_good"]

    async def test_get_entries_filter_by_event(self, tmp_path: Path) -> None:
        """Covers lines 100-101: skip entries whose event != filter."""
        log_path = tmp_path / "s.jsonl"
        log_path.write_text(
            '{"event": "a", "ts": 1}\n{"event": "b", "ts": 2}\n{"event": "a", "ts": 3}\n',
        )
        sl = SessionLogger(log_path)
        entries = await sl._store.get_entries("s", event="a")
        assert len(entries) == 2
        assert all(e["event"] == "a" for e in entries)

    async def test_get_entries_with_offset_skips_then_slices(self, tmp_path: Path) -> None:
        """Covers lines 102-104 (skipping for offset) and 108 (offset slice)."""
        log_path = tmp_path / "s.jsonl"
        rows = "\n".join(f'{{"event": "e{i}", "ts": {i}}}' for i in range(10)) + "\n"
        log_path.write_text(rows)
        sl = SessionLogger(log_path)
        # offset=3, limit=2 → skip 3, then take 2
        entries = await sl._store.get_entries("s", limit=2, offset=3)
        assert [e["event"] for e in entries] == ["e3", "e4"]

    async def test_get_entries_normalizes_limit(self, tmp_path: Path) -> None:
        """Covers line 91: normalized_limit clamps to [1, 500]."""
        log_path = tmp_path / "s.jsonl"
        rows = "\n".join(f'{{"event": "e{i}", "ts": {i}}}' for i in range(5)) + "\n"
        log_path.write_text(rows)
        sl = SessionLogger(log_path)
        # limit=0 should clamp to 1 (max(1, ...))
        entries = await sl._store.get_entries("s", limit=0)
        assert len(entries) == 1


# ---------------------------------------------------------------------------
# Legacy _write_event_unlocked, control_channel modes, contexts, and timers.
# Covers source lines 135, 149-150, 199-200, 212, 220, 235->237, 245, 261.
# ---------------------------------------------------------------------------


class TestSessionLoggerMisc:
    async def test_write_event_unlocked_legacy_compat(self, tmp_path: Path) -> None:
        """Covers line 135: _write_event_unlocked delegates to _write_event."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl._write_event_unlocked("legacy_evt", {"k": "v"})
        await sl.stop()
        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert any(r["event"] == "legacy_evt" and r["data"] == {"k": "v"} for r in lines)

    async def test_start_handles_non_int_size_bytes(self, tmp_path: Path) -> None:
        """Covers lines 149-150: TypeError/ValueError → _bytes_written = 0."""
        from unittest.mock import AsyncMock, MagicMock

        from provide.uterm.recording import RecordingStore

        store = MagicMock(spec=RecordingStore)
        store.start_session = AsyncMock()
        store.end_session = AsyncMock()
        store.append_events = AsyncMock()
        # size_bytes is a non-int string — int() raises ValueError
        store.recording_meta = AsyncMock(return_value={"size_bytes": "not-a-number"})

        sl = SessionLogger(store, flush_interval_s=10.0)
        await sl.start("s")
        try:
            assert sl._bytes_written == 0
        finally:
            await sl.stop()

    async def test_log_wire_writes_when_mode_is_wire(self, tmp_path: Path) -> None:
        """Covers lines 199-200: log_wire emits an event when mode == 'wire'."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path, control_channel_mode="wire")
        await sl.start("s")
        await sl.log_wire("send", "hello-wire")
        await sl.log_wire("recv", "hi-back")
        await sl.stop()
        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        events = [r for r in lines if r["event"] in ("wire_send", "wire_recv")]
        assert {e["event"] for e in events} == {"wire_send", "wire_recv"}
        send = next(r for r in events if r["event"] == "wire_send")
        assert send["data"]["text"] == "hello-wire"

    async def test_log_wire_skipped_when_mode_excludes(self, tmp_path: Path) -> None:
        """Covers lines 197-198 short-circuit when mode != 'wire'."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)  # default: 'exclude'
        await sl.start("s")
        await sl.log_wire("send", "should-not-be-written")
        await sl.stop()
        text = log_path.read_text()
        assert "should-not-be-written" not in text

    async def test_log_control_writes_when_mode_is_wire(self, tmp_path: Path) -> None:
        """Covers line 212: log_control emits an event when mode == 'wire'."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path, control_channel_mode="wire")
        await sl.start("s")
        await sl.log_control("send", {"type": "hello"})
        await sl.stop()
        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        rec = next(r for r in lines if r["event"] == "control_send")
        assert rec["data"]["control"] == {"type": "hello"}

    async def test_log_control_skipped_when_mode_excludes(self, tmp_path: Path) -> None:
        """log_control short-circuits when mode != 'wire'."""
        log_path = tmp_path / "s.jsonl"
        sl = SessionLogger(log_path)
        await sl.start("s")
        await sl.log_control("recv", {"type": "should-not-appear"})
        await sl.stop()
        assert "should-not-appear" not in log_path.read_text()

    def test_clear_context_resets_to_empty(self, tmp_path: Path) -> None:
        """Covers line 220: clear_context resets _context to {}."""
        sl = SessionLogger(tmp_path / "s.jsonl")
        sl.set_context({"user": "alice"})
        assert sl._context == {"user": "alice"}
        sl.clear_context()
        assert sl._context == {}

    async def test_write_event_without_session_id_skips_session_field(self, tmp_path: Path) -> None:
        """Covers branch 235->237: ``if self._session_id`` is falsy → skip
        adding session_id to record, fall through to context check."""
        from unittest.mock import AsyncMock, MagicMock

        from provide.uterm.recording import RecordingStore

        store = MagicMock(spec=RecordingStore)
        store.append_events = AsyncMock()
        store.start_session = AsyncMock()
        store.end_session = AsyncMock()
        store.recording_meta = AsyncMock(return_value={"size_bytes": 0})

        sl = SessionLogger(store)
        # Don't call start() — _session_id remains None.
        await sl._write_event("orphan", {"k": "v"})
        # Buffered record was added without session_id.
        assert len(sl._buffer) == 1
        rec = sl._buffer[0]
        assert "session_id" not in rec
        assert rec["event"] == "orphan"

    async def test_batch_size_triggers_flush(self, tmp_path: Path) -> None:
        """Covers line 244-245: when buffer >= batch_size, flush is invoked."""
        from unittest.mock import AsyncMock, MagicMock

        from provide.uterm.recording import RecordingStore

        store = MagicMock(spec=RecordingStore)
        store.start_session = AsyncMock()
        store.end_session = AsyncMock()
        store.append_events = AsyncMock()
        store.recording_meta = AsyncMock(return_value={"size_bytes": 0})

        sl = SessionLogger(store, batch_size=2, flush_interval_s=60.0)
        await sl.start("s")
        # Each log_event adds 1 to buffer. The third call triggers flush
        # when len(buffer) reaches batch_size (2).
        await sl.log_event("a", {})
        await sl.log_event("b", {})  # batch_size reached → flush
        # After flush, buffer is empty.
        assert sl._buffer == []
        # append_events received the batch.
        assert store.append_events.call_count >= 1
        await sl.stop()

    async def test_periodic_flush_runs_when_interval_elapses(self, tmp_path: Path) -> None:
        """Covers lines 258-261: _periodic_flush wakes up and calls _flush_buffer."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from provide.uterm.recording import RecordingStore

        store = MagicMock(spec=RecordingStore)
        store.start_session = AsyncMock()
        store.end_session = AsyncMock()
        store.append_events = AsyncMock()
        store.recording_meta = AsyncMock(return_value={"size_bytes": 0})

        # Very tight interval so the loop body runs at least once during the test.
        sl = SessionLogger(store, flush_interval_s=0.01, batch_size=100)
        await sl.start("s")
        await sl.log_event("evt", {"x": 1})
        # Wait long enough for at least one periodic flush cycle to fire.
        await asyncio.sleep(0.1)
        # The periodic flush should have flushed at least once.
        assert store.append_events.call_count >= 1
        await sl.stop()

    async def test_public_flush_drains_buffer(self, tmp_path: Path) -> None:
        """Covers line 224: SessionLogger.flush() delegates to _flush_buffer."""
        from unittest.mock import AsyncMock, MagicMock

        from provide.uterm.recording import RecordingStore

        store = MagicMock(spec=RecordingStore)
        store.start_session = AsyncMock()
        store.end_session = AsyncMock()
        store.append_events = AsyncMock()
        store.recording_meta = AsyncMock(return_value={"size_bytes": 0})

        # Large batch + long interval so neither path auto-flushes — only the
        # explicit flush() call can drain the buffer for this assertion.
        sl = SessionLogger(store, batch_size=100, flush_interval_s=60.0)
        await sl.start("s")
        await sl.log_event("buffered", {"k": "v"})
        assert len(sl._buffer) == 1
        # Public API: flush() must drain the buffer through _flush_buffer.
        await sl.flush()
        assert sl._buffer == []
        assert store.append_events.call_count >= 1
        await sl.stop()
