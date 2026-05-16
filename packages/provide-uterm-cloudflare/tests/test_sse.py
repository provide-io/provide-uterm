#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for do/_sse.py — build_sse_response and route_sse."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest
from provide.uterm.cloudflare.do._sse import build_sse_response, route_sse
from provide.uterm.cloudflare.state.store import SqliteStateStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(events: list[dict[str, Any]] | None = None) -> SqliteStateStore:
    conn = sqlite3.connect(":memory:")
    store = SqliteStateStore(conn.execute)
    store.migrate()
    if events:
        for ev in events:
            store.append_event("w1", ev.get("type", "snapshot"), ev.get("data", {}))
    return store


class _Runtime:
    def __init__(self, store: SqliteStateStore, worker_id: str = "w1") -> None:
        self.store = store
        self.worker_id = worker_id
        self.lifecycle_state = "stopped"
        self.meta: dict = {
            "display_name": self.worker_id,
            "connector_type": "unknown",
            "created_at": 0.0,
            "tags": [],
            "visibility": "public",
            "owner": None,
        }


def _req(url: str, *, headers: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(url=url, method="GET", headers=headers or {})


# ---------------------------------------------------------------------------
# build_sse_response
# ---------------------------------------------------------------------------


def test_build_sse_response_empty_events() -> None:
    resp = build_sse_response([])
    assert resp.status == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.body
    assert "retry: 3000" in body


def test_build_sse_response_with_events() -> None:
    events = [{"seq": 1, "type": "snapshot", "data": {"screen": "$ hello"}}, {"seq": 2, "type": "term", "data": {}}]
    resp = build_sse_response(events)
    body = resp.body
    assert "id: 1" in body
    assert "id: 2" in body
    assert "snapshot" in body
    assert "term" in body
    assert "retry: 3000" in body


def test_build_sse_response_custom_retry_ms() -> None:
    resp = build_sse_response([], retry_ms=5000)
    assert "retry: 5000" in resp.body


def test_build_sse_response_event_without_seq() -> None:
    """Events without seq field get empty id."""
    resp = build_sse_response([{"type": "heartbeat"}])
    body = resp.body
    assert "id: " in body


def test_build_sse_response_cache_control_headers() -> None:
    resp = build_sse_response([])
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"


# ---------------------------------------------------------------------------
# route_sse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_sse_session_not_found() -> None:
    store = _make_store()
    runtime = _Runtime(store, worker_id="correct-id")
    req = _req("http://example.com/api/sessions/wrong-id/events/stream")
    resp = await route_sse(runtime, req, str(req.url), "wrong-id")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_route_sse_no_events() -> None:
    store = _make_store()
    runtime = _Runtime(store, worker_id="w1")
    req = _req("http://example.com/api/sessions/w1/events/stream")
    resp = await route_sse(runtime, req, str(req.url), "w1")
    assert resp.status == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "retry:" in resp.body


@pytest.mark.asyncio
async def test_route_sse_returns_events_since() -> None:
    store = _make_store()
    store.append_event("w1", "snapshot", {"screen": "$ test"})
    store.append_event("w1", "snapshot", {"screen": "$ test2"})
    runtime = _Runtime(store, worker_id="w1")
    req = _req("http://example.com/api/sessions/w1/events/stream?after_seq=0")
    resp = await route_sse(runtime, req, str(req.url), "w1")
    assert resp.status == 200
    body = resp.body
    assert "snapshot" in body


@pytest.mark.asyncio
async def test_route_sse_after_seq_filters() -> None:
    store = _make_store()
    e1 = store.append_event("w1", "snapshot", {"screen": "$ first"})
    store.append_event("w1", "snapshot", {"screen": "$ second"})
    seq1 = e1["seq"]
    runtime = _Runtime(store, worker_id="w1")
    req = _req(f"http://example.com/api/sessions/w1/events/stream?after_seq={seq1}")
    resp = await route_sse(runtime, req, str(req.url), "w1")
    body = resp.body
    # Only seq2 should be in response
    assert "second" in body
    assert "first" not in body


@pytest.mark.asyncio
async def test_route_sse_reads_last_event_id_header() -> None:
    store = _make_store()
    store.append_event("w1", "snapshot", {"screen": "$ hdr"})
    runtime = _Runtime(store, worker_id="w1")
    # after_seq=0 via Last-Event-ID header
    req = _req("http://example.com/api/sessions/w1/events/stream", headers={"last-event-id": "0"})
    resp = await route_sse(runtime, req, str(req.url), "w1")
    assert resp.status == 200
    assert "hdr" in resp.body


@pytest.mark.asyncio
async def test_route_sse_invalid_after_seq_defaults_to_zero() -> None:
    store = _make_store()
    store.append_event("w1", "snapshot", {})
    runtime = _Runtime(store, worker_id="w1")
    req = _req("http://example.com/api/sessions/w1/events/stream?after_seq=bad")
    resp = await route_sse(runtime, req, str(req.url), "w1")
    assert resp.status == 200
    body = resp.body
    assert "id: " in body


@pytest.mark.asyncio
async def test_route_sse_negative_after_seq_clamped_to_zero() -> None:
    store = _make_store()
    store.append_event("w1", "snapshot", {})
    runtime = _Runtime(store, worker_id="w1")
    req = _req("http://example.com/api/sessions/w1/events/stream?after_seq=-5")
    resp = await route_sse(runtime, req, str(req.url), "w1")
    assert resp.status == 200
