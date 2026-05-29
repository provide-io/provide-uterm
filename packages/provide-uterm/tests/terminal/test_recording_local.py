#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for LocalFileRecordingStore (JSONL file-backed recordings)."""

from __future__ import annotations

import json
from pathlib import Path

from provide.uterm.recording import LocalFileRecordingStore, RecordingStore


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestLocalFileRecordingStoreProtocol:
    def test_satisfies_protocol(self) -> None:
        store = LocalFileRecordingStore("/tmp")
        assert isinstance(store, RecordingStore)


class TestLocalFileRecordingStoreLifecycle:
    async def test_start_writes_log_start_event(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.start_session("s1", {"who": "alice"})
        path = tmp_path / "s1.jsonl"
        lines = _read_lines(path)
        assert len(lines) == 1
        assert lines[0]["event"] == "log_start"
        assert lines[0]["data"] == {"who": "alice"}
        assert lines[0]["session_id"] == "s1"
        assert "ts" in lines[0]

    async def test_start_creates_parent_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested"
        store = LocalFileRecordingStore(nested)
        await store.start_session("s1", {})
        assert (nested / "s1.jsonl").exists()

    async def test_end_writes_log_stop_event_and_closes(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.start_session("s1", {})
        await store.end_session("s1")
        lines = _read_lines(tmp_path / "s1.jsonl")
        assert lines[-1]["event"] == "log_stop"
        assert lines[-1]["data"] == {}
        assert lines[-1]["session_id"] == "s1"

    async def test_end_without_open_file_is_noop(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        # No start_session, so no file handle is registered.
        await store.end_session("never-started")
        assert not (tmp_path / "never-started.jsonl").exists()


class TestLocalFileRecordingStoreAppend:
    async def test_append_after_start_uses_open_handle(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.start_session("s1", {})
        await store.append_events("s1", [{"ts": 1, "event": "data", "data": "x"}])
        lines = _read_lines(tmp_path / "s1.jsonl")
        assert lines[-1]["event"] == "data"
        assert lines[-1]["data"] == "x"

    async def test_append_without_start_opens_file(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.append_events("s2", [{"event": "a"}, {"event": "b"}])
        lines = _read_lines(tmp_path / "s2.jsonl")
        assert [line["event"] for line in lines] == ["a", "b"]

    async def test_append_multiple_events_preserves_order(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.append_events("s3", [{"event": str(i)} for i in range(5)])
        lines = _read_lines(tmp_path / "s3.jsonl")
        assert [line["event"] for line in lines] == ["0", "1", "2", "3", "4"]


class TestLocalFileRecordingStoreMeta:
    async def test_meta_for_existing_file(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.start_session("s1", {})
        meta = await store.recording_meta("s1")
        path = tmp_path / "s1.jsonl"
        assert meta["session_id"] == "s1"
        assert meta["exists"] is True
        assert meta["path"] == str(path)
        assert meta["size_bytes"] == path.stat().st_size
        assert meta["size_bytes"] > 0

    async def test_meta_for_missing_file(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        meta = await store.recording_meta("missing")
        assert meta["exists"] is False
        assert meta["path"] is None
        assert meta["size_bytes"] == 0


class TestLocalFileRecordingStoreGetEntries:
    async def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        assert await store.get_entries("missing") == []

    async def test_tail_default_returns_last_n(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.append_events("s1", [{"event": "e", "n": i} for i in range(10)])
        out = await store.get_entries("s1", limit=3)
        assert [e["n"] for e in out] == [7, 8, 9]

    async def test_tail_event_filter(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.append_events(
            "s1",
            [{"event": "keep", "n": 1}, {"event": "drop", "n": 2}, {"event": "keep", "n": 3}],
        )
        out = await store.get_entries("s1", event="keep")
        assert [e["n"] for e in out] == [1, 3]

    async def test_tail_skips_malformed_json(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        path = tmp_path / "s1.jsonl"
        path.write_text('{"event": "good", "n": 1}\nnot json\n{"event": "good", "n": 2}\n', encoding="utf-8")
        out = await store.get_entries("s1")
        assert [e["n"] for e in out] == [1, 2]

    async def test_offset_pagination(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.append_events("s1", [{"event": "e", "n": i} for i in range(10)])
        out = await store.get_entries("s1", limit=3, offset=2)
        assert [e["n"] for e in out] == [2, 3, 4]

    async def test_offset_with_event_filter(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.append_events(
            "s1",
            [{"event": "keep" if i % 2 == 0 else "drop", "n": i} for i in range(10)],
        )
        out = await store.get_entries("s1", limit=2, offset=1, event="keep")
        # keep events are n=0,2,4,6,8; skip 1 -> start at 2; take 2 -> 2,4
        assert [e["n"] for e in out] == [2, 4]

    async def test_offset_skips_malformed_json(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        path = tmp_path / "s1.jsonl"
        path.write_text('{"event": "e", "n": 0}\ngarbage\n{"event": "e", "n": 1}\n', encoding="utf-8")
        out = await store.get_entries("s1", offset=0)
        assert [e["n"] for e in out] == [0, 1]

    async def test_offset_stops_at_limit(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.append_events("s1", [{"event": "e", "n": i} for i in range(10)])
        out = await store.get_entries("s1", limit=1, offset=0)
        assert [e["n"] for e in out] == [0]

    async def test_limit_clamped_to_one(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.append_events("s1", [{"event": "e", "n": i} for i in range(5)])
        out = await store.get_entries("s1", limit=0)
        assert len(out) == 1

    async def test_limit_clamped_to_five_hundred(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.append_events("s1", [{"event": "e", "n": i} for i in range(600)])
        out = await store.get_entries("s1", limit=1000)
        assert len(out) == 500


class TestLocalFileRecordingStoreGetPath:
    async def test_get_path_existing(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.start_session("s1", {})
        result = await store.get_path("s1")
        assert result == tmp_path / "s1.jsonl"

    async def test_get_path_missing_returns_none(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        assert await store.get_path("missing") is None


class TestLocalFileRecordingStoreLocking:
    """Per-session locks must be keyed by session_id, not a shared constant."""

    async def test_start_session_registers_lock_under_session_id(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.start_session("s1", {})
        assert "s1" in store._locks
        assert None not in store._locks

    async def test_append_uses_per_session_lock(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.append_events("s2", [{"event": "e"}])
        assert "s2" in store._locks
        assert None not in store._locks

    async def test_end_uses_per_session_lock(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.start_session("s3", {})
        store._locks.clear()
        await store.end_session("s3")
        assert "s3" in store._locks
        assert None not in store._locks


class TestLocalFileRecordingStoreHandleReuse:
    """append_events must reuse the cached open handle from start_session."""

    async def test_append_reuses_started_handle(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.start_session("s1", {})
        started_handle = store._files["s1"]
        await store.append_events("s1", [{"event": "e"}])
        # The cached handle object must be the same one start_session opened —
        # kills ``f = None`` / ``f = self._files.get(None)`` / ``_files[sid] = None``.
        assert store._files["s1"] is started_handle

    async def test_append_without_start_caches_opened_handle(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.append_events("s5", [{"event": "e"}])
        assert "s5" in store._files
        assert store._files["s5"] is not None


class TestLocalFileRecordingStoreStopEventShape:
    async def test_stop_event_has_ts_key(self, tmp_path: Path) -> None:
        store = LocalFileRecordingStore(tmp_path)
        await store.start_session("s1", {})
        await store.end_session("s1")
        lines = _read_lines(tmp_path / "s1.jsonl")
        assert "ts" in lines[-1]
        assert lines[-1]["data"] == {}
