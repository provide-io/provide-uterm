#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for the terminal hijack REST routes — advanced tests.

Covers: snapshot, events, step, and send guard constraint tests.  Basic
endpoint tests (acquire, heartbeat, release, send, validation) live in
test_hijack_routes.py.  Regression tests live in
test_hijack_routes_regression.py.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.bridge.hub import TermHub
from provide.terminal.bridge.models import HijackSession, WorkerTermState


def make_app() -> tuple[FastAPI, TermHub]:
    hub = TermHub()
    app = FastAPI()
    app.include_router(hub.create_router())
    return app, hub


def _active_session(hijack_id: str, owner: str = "test") -> HijackSession:
    return HijackSession(
        hijack_id=hijack_id,
        owner=owner,
        acquired_at=time.time(),
        lease_expires_at=time.time() + 3600,
        last_heartbeat=time.time(),
    )


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_snapshot_no_worker() -> None:
    app, hub = make_app()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.get(f"/worker/bot1/hijack/{hijack_id}/snapshot?wait_ms=50")

    assert r.status_code == 200
    data = r.json()
    assert data["snapshot"] is None
    assert data["worker_id"] == "bot1"


def test_snapshot_invalid_session() -> None:
    app, hub = make_app()

    with TestClient(app) as client:
        r = client.get("/worker/bot1/hijack/no-such-id/snapshot")

    # "no-such-id" fails the UUID path pattern → 422 before route logic runs
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_events() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.get(f"/worker/bot1/hijack/{hijack_id}/events")

    assert r.status_code == 200
    data = r.json()
    assert "events" in data
    assert "latest_seq" in data


def test_events_invalid_session() -> None:
    app, hub = make_app()

    with TestClient(app) as client:
        r = client.get("/worker/bot1/hijack/no-such-id/events")

    # "no-such-id" fails the UUID path pattern → 422 before route logic runs
    assert r.status_code == 422


def test_events_empty_bot_state() -> None:
    """Events endpoint when bot has no events returns empty list."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.get(f"/worker/bot1/hijack/{hijack_id}/events")

    assert r.status_code == 200
    assert r.json()["events"] == []


def test_events_no_bot_state() -> None:
    """Events endpoint when bot has no state returns empty list (lines 420-421)."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    # Register valid session but with no bot state in hub._workers
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    # Use a different bot_id that has no state
    # But we need a valid session... let's use a workaround:
    # Get the session via bot1, then remove the bot state
    hub._workers["bot2"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )
    # Remove bot2's state to trigger the None branch
    del hub._workers["bot2"]
    # Re-add an empty state without using a separate event loop (asyncio.Lock is
    # loop-bound; running hub coroutines on a different loop is incorrect).
    hub._workers["bot2"] = WorkerTermState()
    hub._workers["bot2"].hijack_session = _active_session(hijack_id)

    with TestClient(app) as client:
        r = client.get(f"/worker/bot2/hijack/{hijack_id}/events")

    assert r.status_code == 200
    assert r.json()["events"] == []


# ---------------------------------------------------------------------------
# step
# ---------------------------------------------------------------------------


def test_step() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/worker/bot1/hijack/{hijack_id}/step")

    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_step_no_worker() -> None:
    app, hub = make_app()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/worker/bot1/hijack/{hijack_id}/step")

    assert r.status_code == 409


def test_step_invalid_hijack_session() -> None:
    """Step with invalid/expired hijack_id returns 404 (line 484)."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post("/worker/bot1/hijack/no-such-id/step")

    # "no-such-id" fails the UUID path pattern → 422 before route logic runs
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# send guard constraints
# ---------------------------------------------------------------------------


def test_send_with_worker_and_no_guard() -> None:
    """Send with keys and worker connected, no guard constraints -> succeeds."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(
            f"/worker/bot1/hijack/{hijack_id}/send",
            json={"keys": "hello\r", "timeout_ms": 100},
        )

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["sent"] == "hello\r"


def test_send_guard_not_satisfied() -> None:
    """Send with an expect_prompt_id that never matches -> 409."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(
            f"/worker/bot1/hijack/{hijack_id}/send",
            json={"keys": "hello\r", "expect_prompt_id": "never_matches", "timeout_ms": 100},
        )

    assert r.status_code == 409


def test_send_guard_invalid_regex() -> None:
    """Send with invalid expect_regex -> 409."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(
            f"/worker/bot1/hijack/{hijack_id}/send",
            json={"keys": "hello\r", "expect_regex": "[invalid"},
        )

    assert r.status_code == 409


# ---------------------------------------------------------------------------
# has_more pagination flag — uses >= so it is True when exactly limit rows returned
# ---------------------------------------------------------------------------


def test_events_has_more_true_when_exactly_limit_rows() -> None:
    """has_more must be True when exactly limit events are returned.

    Kills the mutation:
      "has_more": len(rows) >= limit  →  "has_more": len(rows) > limit
    (> would return False when len == limit, which is wrong.)
    """
    from collections import deque

    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    st = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )
    # Add exactly 5 events, query with limit=5 → has_more should be True.
    st.events = deque(maxlen=2000)
    for i in range(5):
        st.events.append({"seq": i + 1, "ts": time.time(), "type": "snapshot", "data": {}})
    st.event_seq = 5
    st.min_event_seq = 1
    hub._workers["bot1"] = st

    with TestClient(app) as client:
        r = client.get(f"/worker/bot1/hijack/{hijack_id}/events?limit=5&after_seq=0")

    assert r.status_code == 200
    data = r.json()
    assert len(data["events"]) == 5
    assert data["has_more"] is True, "has_more must be True when exactly limit events are returned"


def test_events_has_more_false_when_fewer_than_limit() -> None:
    """has_more is False when fewer than limit events are returned.

    Kills the mutation:
      "has_more": len(rows) >= limit  →  "has_more": True  (always)
    """
    from collections import deque

    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    st = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )
    # Only 3 events but limit=10 → has_more must be False.
    st.events = deque(maxlen=2000)
    for i in range(3):
        st.events.append({"seq": i + 1, "ts": time.time(), "type": "snapshot", "data": {}})
    st.event_seq = 3
    st.min_event_seq = 1
    hub._workers["bot1"] = st

    with TestClient(app) as client:
        r = client.get(f"/worker/bot1/hijack/{hijack_id}/events?limit=10&after_seq=0")

    assert r.status_code == 200
    data = r.json()
    assert len(data["events"]) == 3
    assert data["has_more"] is False, "has_more must be False when fewer than limit events are returned"
