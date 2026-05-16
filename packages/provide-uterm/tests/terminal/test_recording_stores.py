#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for InMemoryRecordingStore and NullRecordingStore."""

from __future__ import annotations

from provide.uterm.recording import InMemoryRecordingStore, NullRecordingStore, RecordingStore


class TestInMemoryRecordingStoreProtocol:
    def test_satisfies_protocol(self) -> None:
        store = InMemoryRecordingStore()
        assert isinstance(store, RecordingStore)


class TestInMemoryRecordingStoreLifecycle:
    async def test_start_creates_session(self) -> None:
        store = InMemoryRecordingStore()
        await store.start_session("s1", {"user": "alice"})
        meta = await store.recording_meta("s1")
        assert meta["session_id"] == "s1"
        assert meta["exists"] is True
        assert meta["size_bytes"] > 0

    async def test_end_marks_session_inactive(self) -> None:
        store = InMemoryRecordingStore()
        await store.start_session("s1", {})
        assert store._sessions["s1"]["active"] is True
        await store.end_session("s1")
        assert store._sessions["s1"]["active"] is False

    async def test_start_appends_log_start_event(self) -> None:
        store = InMemoryRecordingStore()
        await store.start_session("s1", {"key": "val"})
        entries = await store.get_entries("s1")
        assert len(entries) == 1
        assert entries[0]["event"] == "log_start"
        assert entries[0]["data"] == {"key": "val"}
        assert entries[0]["session_id"] == "s1"
        assert "ts" in entries[0]

    async def test_end_appends_log_stop_event(self) -> None:
        store = InMemoryRecordingStore()
        await store.start_session("s1", {})
        await store.end_session("s1")
        entries = await store.get_entries("s1")
        assert entries[-1]["event"] == "log_stop"
        assert entries[-1]["session_id"] == "s1"


class TestInMemoryRecordingStoreAppend:
    async def test_append_events(self) -> None:
        store = InMemoryRecordingStore()
        await store.start_session("s1", {})
        events = [
            {"ts": 1.0, "event": "read", "data": {"raw": "hello"}},
            {"ts": 2.0, "event": "send", "data": {"keys": "x"}},
        ]
        await store.append_events("s1", events)
        entries = await store.get_entries("s1")
        # log_start + 2 appended
        assert len(entries) == 3

    async def test_append_to_nonexistent_session(self) -> None:
        store = InMemoryRecordingStore()
        await store.append_events("no-session", [{"ts": 1.0, "event": "test", "data": {}}])
        entries = await store.get_entries("no-session")
        assert len(entries) == 1


class TestInMemoryRecordingStoreMeta:
    async def test_meta_nonexistent_session(self) -> None:
        store = InMemoryRecordingStore()
        meta = await store.recording_meta("missing")
        assert meta["session_id"] == "missing"
        assert meta["exists"] is False
        assert meta["size_bytes"] == 0

    async def test_meta_size_grows_with_events(self) -> None:
        store = InMemoryRecordingStore()
        await store.start_session("s1", {})
        meta_before = await store.recording_meta("s1")
        await store.append_events("s1", [{"ts": 1.0, "event": "read", "data": {"payload": "x" * 100}}])
        meta_after = await store.recording_meta("s1")
        assert meta_after["size_bytes"] > meta_before["size_bytes"]


class TestInMemoryRecordingStoreGetEntries:
    async def test_tail_behaviour_default(self) -> None:
        store = InMemoryRecordingStore()
        await store.start_session("s1", {})
        for i in range(10):
            await store.append_events("s1", [{"ts": float(i), "event": "read", "data": {"i": i}}])
        # Default limit=200, all 11 events (1 start + 10 appended) fit
        entries = await store.get_entries("s1")
        assert len(entries) == 11

    async def test_tail_with_small_limit(self) -> None:
        store = InMemoryRecordingStore()
        await store.start_session("s1", {})
        for i in range(10):
            await store.append_events("s1", [{"ts": float(i), "event": "read", "data": {"i": i}}])
        entries = await store.get_entries("s1", limit=3)
        assert len(entries) == 3
        # Should be the last 3 events
        assert entries[-1]["data"]["i"] == 9

    async def test_offset_pagination(self) -> None:
        store = InMemoryRecordingStore()
        await store.start_session("s1", {})
        for i in range(5):
            await store.append_events("s1", [{"ts": float(i), "event": "read", "data": {"i": i}}])
        # Skip log_start (offset=1), take 2
        entries = await store.get_entries("s1", limit=2, offset=1)
        assert len(entries) == 2
        assert entries[0]["data"]["i"] == 0
        assert entries[1]["data"]["i"] == 1

    async def test_event_filter(self) -> None:
        store = InMemoryRecordingStore()
        await store.start_session("s1", {})
        await store.append_events(
            "s1",
            [
                {"ts": 1.0, "event": "read", "data": {}},
                {"ts": 2.0, "event": "send", "data": {}},
                {"ts": 3.0, "event": "read", "data": {}},
            ],
        )
        entries = await store.get_entries("s1", event="send")
        assert len(entries) == 1
        assert entries[0]["event"] == "send"

    async def test_event_filter_with_offset(self) -> None:
        store = InMemoryRecordingStore()
        await store.start_session("s1", {})
        await store.append_events(
            "s1",
            [
                {"ts": 1.0, "event": "read", "data": {"i": 0}},
                {"ts": 2.0, "event": "read", "data": {"i": 1}},
                {"ts": 3.0, "event": "read", "data": {"i": 2}},
            ],
        )
        # 3 read events match; offset=1 skips the first, limit=1 takes one
        entries = await store.get_entries("s1", limit=1, offset=1, event="read")
        assert len(entries) == 1
        assert entries[0]["data"]["i"] == 1

    async def test_nonexistent_session_returns_empty(self) -> None:
        store = InMemoryRecordingStore()
        entries = await store.get_entries("no-such")
        assert entries == []

    async def test_limit_clamped_to_one(self) -> None:
        store = InMemoryRecordingStore()
        await store.start_session("s1", {})
        await store.append_events("s1", [{"ts": 1.0, "event": "read", "data": {}}])
        entries = await store.get_entries("s1", limit=0)
        assert len(entries) == 1

    async def test_limit_clamped_to_five_hundred(self) -> None:
        store = InMemoryRecordingStore()
        await store.start_session("s1", {})
        for i in range(600):
            await store.append_events("s1", [{"ts": float(i), "event": "read", "data": {}}])
        entries = await store.get_entries("s1", limit=9999)
        assert len(entries) == 500


class TestInMemoryRecordingStoreGetPath:
    async def test_get_path_returns_none(self) -> None:
        store = InMemoryRecordingStore()
        assert await store.get_path("anything") is None


class TestNullRecordingStoreProtocol:
    def test_satisfies_protocol(self) -> None:
        store = NullRecordingStore()
        assert isinstance(store, RecordingStore)


class TestNullRecordingStoreOperations:
    async def test_start_session_is_noop(self) -> None:
        store = NullRecordingStore()
        result = await store.start_session("s1", {"key": "value"})
        assert result is None

    async def test_append_events_is_noop(self) -> None:
        store = NullRecordingStore()
        result = await store.append_events("s1", [{"ts": 1.0, "event": "test", "data": {}}])
        assert result is None

    async def test_end_session_is_noop(self) -> None:
        store = NullRecordingStore()
        result = await store.end_session("s1")
        assert result is None

    async def test_recording_meta_returns_not_exists(self) -> None:
        store = NullRecordingStore()
        meta = await store.recording_meta("s1")
        assert meta == {"session_id": "s1", "exists": False, "size_bytes": 0}

    async def test_get_entries_returns_empty(self) -> None:
        store = NullRecordingStore()
        entries = await store.get_entries("s1")
        assert entries == []

    async def test_get_entries_with_params_returns_empty(self) -> None:
        store = NullRecordingStore()
        entries = await store.get_entries("s1", limit=50, offset=10, event="read")
        assert entries == []

    async def test_get_path_returns_none(self) -> None:
        store = NullRecordingStore()
        assert await store.get_path("s1") is None

    async def test_full_lifecycle_is_noop(self) -> None:
        """Entire lifecycle produces no data and no errors."""
        store = NullRecordingStore()
        await store.start_session("s1", {"user": "bob"})
        await store.append_events("s1", [{"ts": 1.0, "event": "read", "data": {}}])
        await store.end_session("s1")

        meta = await store.recording_meta("s1")
        assert meta["exists"] is False
        entries = await store.get_entries("s1")
        assert entries == []
        assert await store.get_path("s1") is None
