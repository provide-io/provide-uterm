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
from provide.uterm.cloudflare.api.http_routes._shared import _session_status_item
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


def test_list_recording_different_worker_isolated() -> None:
    """Events from one worker don't appear in another worker's recording."""
    store = _make_store(3, worker_id="w1")
    store.append_event("w2", "snapshot", {"screen": "other"})
    entries_w1 = store.list_recording_entries("w1")
    entries_w2 = store.list_recording_entries("w2")
    assert len(entries_w1) == 3
    assert len(entries_w2) == 1


# ---------------------------------------------------------------------------
# Route: additional coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_recording_entries_wrong_session() -> None:
    """Entries endpoint returns 404 for wrong session_id."""
    runtime = _Runtime(_make_store(5))
    url = "http://localhost/api/sessions/wrong-id/recording/entries"
    resp = await route_recording(runtime, None, url, "wrong-id", "entries")
    status, _body = _parse_response(resp)
    assert status == 404


@pytest.mark.asyncio
async def test_route_recording_entries_tail_order() -> None:
    """Route-level: tail mode returns entries in ascending order."""
    runtime = _Runtime(_make_store(5))
    url = "http://localhost/api/sessions/w1/recording/entries?limit=3"
    resp = await route_recording(runtime, None, url, "w1", "entries")
    status, body = _parse_response(resp)
    assert status == 200
    assert len(body) == 3
    # Ascending: i=2, i=3, i=4 (last 3)
    assert body[0]["data"]["i"] < body[1]["data"]["i"] < body[2]["data"]["i"]


@pytest.mark.asyncio
async def test_route_recording_entries_snapshot_has_screen() -> None:
    """Replay frontend compatibility: snapshot entries have data.screen."""
    runtime = _Runtime(_make_store(2))
    url = "http://localhost/api/sessions/w1/recording/entries?event=snapshot"
    resp = await route_recording(runtime, None, url, "w1", "entries")
    status, body = _parse_response(resp)
    assert status == 200
    assert len(body) >= 1
    assert "screen" in body[0]["data"]


@pytest.mark.asyncio
async def test_dispatch_recording_entries_via_route_http() -> None:
    """GET /recording/entries dispatched correctly through route_http."""
    runtime = _Runtime(_make_store(5))
    req = SimpleNamespace(
        url="http://localhost/api/sessions/w1/recording/entries?limit=2",
        method="GET",
        headers={},
    )
    resp = await route_http(runtime, req)
    status, body = _parse_response(resp)
    assert status == 200
    assert isinstance(body, list)
    assert len(body) == 2


# ---------------------------------------------------------------------------
# Status item: recording_enabled / recording_available
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Store: session metadata persistence
# ---------------------------------------------------------------------------


def test_save_and_load_session_meta_roundtrip() -> None:
    store = _make_store(0)
    meta = {
        "display_name": "My Session",
        "connector_type": "ssh",
        "created_at": 1234567890.0,
        "tags": ["prod", "web"],
        "visibility": "private",
        "owner": "alice",
    }
    store.save_session_meta("w1", meta)
    loaded = store.load_session_meta("w1")
    assert loaded is not None
    assert loaded["display_name"] == "My Session"
    assert loaded["connector_type"] == "ssh"
    assert loaded["created_at"] == 1234567890.0
    assert loaded["tags"] == ["prod", "web"]
    assert loaded["visibility"] == "private"
    assert loaded["owner"] == "alice"


def test_load_session_meta_returns_none_when_missing() -> None:
    store = _make_store(0)
    assert store.load_session_meta("nonexistent") is None


def test_save_session_meta_upsert() -> None:
    store = _make_store(0)
    store.save_session_meta("w1", {"display_name": "v1", "connector_type": "telnet"})
    store.save_session_meta("w1", {"display_name": "v2", "connector_type": "ssh"})
    loaded = store.load_session_meta("w1")
    assert loaded is not None
    assert loaded["display_name"] == "v2"
    assert loaded["connector_type"] == "ssh"


def test_save_session_meta_defaults() -> None:
    """Missing keys in meta dict get sensible defaults."""
    store = _make_store(0)
    store.save_session_meta("w1", {})
    loaded = store.load_session_meta("w1")
    assert loaded is not None
    assert loaded["display_name"] == "w1"  # falls back to worker_id
    assert loaded["connector_type"] == "unknown"
    assert loaded["tags"] == []
    assert loaded["visibility"] == "public"


def test_status_item_uses_meta() -> None:
    """Status item reflects metadata from runtime.meta."""
    runtime = _Runtime(_make_store(0))
    runtime.meta = {
        "display_name": "Custom Name",
        "connector_type": "ssh",
        "created_at": 1234567890.0,
        "tags": ["test"],
        "visibility": "private",
        "owner": "bob",
    }
    item = _session_status_item(runtime)
    assert item["display_name"] == "Custom Name"
    assert item["created_at"] == 1234567890.0
    assert item["connector_type"] == "ssh"
    assert item["tags"] == ["test"]
    assert item["visibility"] == "private"
    assert item["owner"] == "bob"


# ---------------------------------------------------------------------------
# Status item: recording_enabled / recording_available
# ---------------------------------------------------------------------------


def test_status_item_recording_no_events() -> None:
    runtime = _Runtime(_make_store(0))
    item = _session_status_item(runtime)
    assert item["recording_enabled"] is True
    assert item["recording_available"] is False


def test_status_item_recording_with_events() -> None:
    runtime = _Runtime(_make_store(3))
    item = _session_status_item(runtime)
    assert item["recording_enabled"] is True
    assert item["recording_available"] is True
