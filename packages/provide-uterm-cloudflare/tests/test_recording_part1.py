#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for CF recording: store methods, route handlers, and status item."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest
from provide.uterm.cloudflare.api.http_routes._dispatch import route_http
from provide.uterm.cloudflare.api.http_routes._recording import route_recording
from provide.uterm.cloudflare.state.store import SqliteStateStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(n_events: int = 0, *, worker_id: str = "w1") -> SqliteStateStore:
    conn = sqlite3.connect(":memory:")
    store = SqliteStateStore(conn.execute)
    store.migrate()
    for i in range(n_events):
        etype = "snapshot" if i % 2 == 0 else "term"
        store.append_event(worker_id, etype, {"screen": f"screen-{i}", "i": i})
    return store


class _HijackStub:
    session: object = None


class _Runtime:
    def __init__(self, store: SqliteStateStore, worker_id: str = "w1") -> None:
        self.store = store
        self.worker_id = worker_id
        self.meta: dict = {
            "display_name": self.worker_id,
            "connector_type": "unknown",
            "created_at": 0.0,
            "tags": [],
            "visibility": "public",
            "owner": None,
        }
        self.worker_ws = None
        self.lifecycle_state = "stopped"
        self.input_mode = "open"
        self.hijack = _HijackStub()


def _parse_response(resp: Any) -> tuple[int, Any]:
    """Extract (status, parsed_body) from a json_response object."""
    status = getattr(resp, "status", 200)
    body_raw = getattr(resp, "body", b"")
    if isinstance(body_raw, bytes):
        body_raw = body_raw.decode()
    return status, json.loads(body_raw)


# ---------------------------------------------------------------------------
# Store: count_events
# ---------------------------------------------------------------------------


def test_count_events_empty() -> None:
    store = _make_store(0)
    assert store.count_events("w1") == 0


def test_count_events_after_appends() -> None:
    store = _make_store(5)
    assert store.count_events("w1") == 5


def test_count_events_different_worker() -> None:
    store = _make_store(3, worker_id="w1")
    store.append_event("w2", "snapshot", {"screen": "x"})
    assert store.count_events("w1") == 3
    assert store.count_events("w2") == 1


# ---------------------------------------------------------------------------
# Store: list_recording_entries — tail mode (offset=None)
# ---------------------------------------------------------------------------


def test_list_recording_tail_default() -> None:
    store = _make_store(5)
    entries = store.list_recording_entries("w1")
    assert len(entries) == 5
    # Must have {ts, event, data} keys
    for e in entries:
        assert "ts" in e
        assert "event" in e
        assert "data" in e
    # Ascending order
    assert entries[0]["data"]["i"] == 0
    assert entries[-1]["data"]["i"] == 4


def test_list_recording_tail_with_limit() -> None:
    store = _make_store(10)
    entries = store.list_recording_entries("w1", limit=3)
    assert len(entries) == 3
    # Should be last 3 entries (tail), ascending
    assert entries[0]["data"]["i"] == 7
    assert entries[-1]["data"]["i"] == 9


def test_list_recording_tail_with_event_filter() -> None:
    store = _make_store(6)
    entries = store.list_recording_entries("w1", event="snapshot")
    # Events 0, 2, 4 are snapshots (even indices)
    assert len(entries) == 3
    assert all(e["event"] == "snapshot" for e in entries)


# ---------------------------------------------------------------------------
# Store: list_recording_entries — offset mode
# ---------------------------------------------------------------------------


def test_list_recording_offset_zero() -> None:
    store = _make_store(5)
    entries = store.list_recording_entries("w1", offset=0, limit=2)
    assert len(entries) == 2
    assert entries[0]["data"]["i"] == 0
    assert entries[1]["data"]["i"] == 1


def test_list_recording_offset_skip() -> None:
    store = _make_store(5)
    entries = store.list_recording_entries("w1", offset=3, limit=10)
    assert len(entries) == 2
    assert entries[0]["data"]["i"] == 3
    assert entries[1]["data"]["i"] == 4


def test_list_recording_offset_with_event_filter() -> None:
    store = _make_store(10)
    entries = store.list_recording_entries("w1", offset=0, limit=2, event="term")
    assert len(entries) == 2
    assert all(e["event"] == "term" for e in entries)


def test_list_recording_empty() -> None:
    store = _make_store(0)
    entries = store.list_recording_entries("w1")
    assert entries == []


def test_list_recording_limit_clamped() -> None:
    store = _make_store(3)
    entries = store.list_recording_entries("w1", limit=9999)
    assert len(entries) == 3  # clamped to 500, but only 3 exist


# ---------------------------------------------------------------------------
# Route: GET /recording (metadata)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_recording_meta_empty() -> None:
    runtime = _Runtime(_make_store(0))
    resp = await route_recording(runtime, None, "", "w1", None)
    status, body = _parse_response(resp)
    assert status == 200
    assert body["session_id"] == "w1"
    assert body["enabled"] is True
    assert body["entry_count"] == 0
    assert body["exists"] is False


@pytest.mark.asyncio
async def test_route_recording_meta_with_events() -> None:
    runtime = _Runtime(_make_store(7))
    resp = await route_recording(runtime, None, "", "w1", None)
    status, body = _parse_response(resp)
    assert status == 200
    assert body["entry_count"] == 7
    assert body["exists"] is True


@pytest.mark.asyncio
async def test_route_recording_wrong_session() -> None:
    runtime = _Runtime(_make_store(0))
    resp = await route_recording(runtime, None, "", "wrong-id", None)
    status, body = _parse_response(resp)
    assert status == 404


# ---------------------------------------------------------------------------
# Route: GET /recording/entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_recording_entries_default() -> None:
    runtime = _Runtime(_make_store(5))
    url = "http://localhost/api/sessions/w1/recording/entries"
    resp = await route_recording(runtime, None, url, "w1", "entries")
    status, body = _parse_response(resp)
    assert status == 200
    assert isinstance(body, list)
    assert len(body) == 5
    assert body[0]["event"] in ("snapshot", "term")
    assert "data" in body[0]


@pytest.mark.asyncio
async def test_route_recording_entries_with_limit() -> None:
    runtime = _Runtime(_make_store(10))
    url = "http://localhost/api/sessions/w1/recording/entries?limit=3"
    resp = await route_recording(runtime, None, url, "w1", "entries")
    status, body = _parse_response(resp)
    assert status == 200
    assert len(body) == 3


@pytest.mark.asyncio
async def test_route_recording_entries_with_offset() -> None:
    runtime = _Runtime(_make_store(5))
    url = "http://localhost/api/sessions/w1/recording/entries?offset=0&limit=2"
    resp = await route_recording(runtime, None, url, "w1", "entries")
    status, body = _parse_response(resp)
    assert status == 200
    assert len(body) == 2
    assert body[0]["data"]["i"] == 0


@pytest.mark.asyncio
async def test_route_recording_entries_event_filter() -> None:
    runtime = _Runtime(_make_store(10))
    url = "http://localhost/api/sessions/w1/recording/entries?event=snapshot"
    resp = await route_recording(runtime, None, url, "w1", "entries")
    status, body = _parse_response(resp)
    assert status == 200
    assert all(e["event"] == "snapshot" for e in body)


@pytest.mark.asyncio
async def test_route_recording_unknown_sub() -> None:
    runtime = _Runtime(_make_store(0))
    resp = await route_recording(runtime, None, "", "w1", "something_else")
    status, _body = _parse_response(resp)
    assert status == 404


@pytest.mark.asyncio
async def test_route_recording_download() -> None:
    """GET /recording/download returns JSONL with {ts, event, data} per line."""
    runtime = _Runtime(_make_store(3))
    resp = await route_recording(runtime, None, "", "w1", "download")
    assert getattr(resp, "status", 200) == 200
    headers = getattr(resp, "headers", {}) or {}
    assert "ndjson" in headers.get("content-type", "")
    assert "w1.jsonl" in headers.get("content-disposition", "")
    body = getattr(resp, "body", "")
    lines = [line for line in body.strip().split("\n") if line]
    assert len(lines) == 3
    import json

    for line in lines:
        entry = json.loads(line)
        assert "ts" in entry
        assert "event" in entry
        assert "data" in entry


# ---------------------------------------------------------------------------
# Status item: recording_enabled / recording_available
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_recording_entries_bad_limit() -> None:
    """Non-integer limit falls back to default 200."""
    runtime = _Runtime(_make_store(3))
    url = "http://localhost/api/sessions/w1/recording/entries?limit=abc&offset=xyz"
    resp = await route_recording(runtime, None, url, "w1", "entries")
    status, body = _parse_response(resp)
    assert status == 200
    assert len(body) == 3  # default limit=200 → returns all 3


@pytest.mark.asyncio
async def test_dispatch_recording_get_via_route_http() -> None:
    """GET /recording is dispatched correctly through route_http."""
    runtime = _Runtime(_make_store(3))
    req = SimpleNamespace(url="http://localhost/api/sessions/w1/recording", method="GET", headers={})
    resp = await route_http(runtime, req)
    status, body = _parse_response(resp)
    assert status == 200
    assert body["entry_count"] == 3


@pytest.mark.asyncio
async def test_dispatch_recording_post_falls_through() -> None:
    """POST to /recording URL is not handled by the recording route (GET-only)."""
    runtime = _Runtime(_make_store(0))
    req = SimpleNamespace(url="http://localhost/api/sessions/w1/recording", method="POST", headers={})
    resp = await route_http(runtime, req)
    status, _body = _parse_response(resp)
    # Falls through to session_match or 404
    assert status in (404, 405)


# ---------------------------------------------------------------------------
# Store: edge cases
# ---------------------------------------------------------------------------


def test_list_recording_event_filter_no_matches() -> None:
    store = _make_store(5)
    entries = store.list_recording_entries("w1", event="nonexistent_type")
    assert entries == []


def test_list_recording_offset_beyond_end() -> None:
    store = _make_store(3)
    entries = store.list_recording_entries("w1", offset=100, limit=10)
    assert entries == []


def test_list_recording_limit_zero_clamped_to_one() -> None:
    store = _make_store(5)
    entries = store.list_recording_entries("w1", limit=0)
    assert len(entries) == 1  # clamped to min 1


def test_list_recording_tail_event_filter_with_limit() -> None:
    """Tail mode + event filter + limit: returns last N matching events."""
    store = _make_store(10)  # 5 snapshots (even), 5 terms (odd)
    entries = store.list_recording_entries("w1", limit=2, event="snapshot")
    assert len(entries) == 2
    assert all(e["event"] == "snapshot" for e in entries)
    # Last two snapshots: i=6 and i=8
    assert entries[0]["data"]["i"] == 6
    assert entries[1]["data"]["i"] == 8


def test_list_recording_entry_format_has_screen() -> None:
    """Snapshot entries must have data.screen — replay frontend depends on this."""
    store = _make_store(1)  # event 0 is a snapshot
    entries = store.list_recording_entries("w1")
    assert len(entries) == 1
    assert entries[0]["event"] == "snapshot"
    assert "screen" in entries[0]["data"]
    assert entries[0]["data"]["screen"] == "screen-0"


def test_list_recording_ts_is_float() -> None:
    store = _make_store(1)
    entries = store.list_recording_entries("w1")
    assert isinstance(entries[0]["ts"], float)
