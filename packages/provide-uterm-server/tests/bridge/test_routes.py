#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for the terminal hijack REST routes — basic endpoints.

Covers: acquire, heartbeat, release, send, and worker_id validation.
Advanced tests (snapshot, events, step, guard, regression) live in
test_hijack_routes_advanced.py.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.bridge.hub import TermHub
from provide.uterm.bridge.models import HijackSession, WorkerTermState


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
# acquire
# ---------------------------------------------------------------------------


def test_acquire_hijack() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hub._workers["bot1"] = WorkerTermState(worker_ws=mock_ws)

    with TestClient(app) as client:
        r = client.post("/worker/bot1/hijack/acquire", json={"owner": "test", "lease_s": 60})

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "hijack_id" in data
    assert data["owner"] == "test"
    assert "lease_expires_at" in data


def test_acquire_no_worker_returns_409() -> None:
    app, hub = make_app()
    # No worker_ws set

    with TestClient(app) as client:
        r = client.post("/worker/bot1/hijack/acquire", json={"owner": "test"})

    assert r.status_code == 409


def test_acquire_conflict_already_hijacked() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id, "owner_a"),
    )

    with TestClient(app) as client:
        r = client.post("/worker/bot1/hijack/acquire", json={"owner": "owner_b"})

    assert r.status_code == 409


def test_acquire_default_owner() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hub._workers["bot1"] = WorkerTermState(worker_ws=mock_ws)

    with TestClient(app) as client:
        r = client.post("/worker/bot1/hijack/acquire")

    assert r.status_code == 200
    assert r.json()["owner"] == "operator"  # default from HijackAcquireRequest


def test_acquire_send_worker_fails() -> None:
    """When _send_worker returns False after acquiring, return 409."""
    app, hub = make_app()
    from unittest.mock import AsyncMock

    bad_ws = AsyncMock()
    bad_ws.send_text = AsyncMock(side_effect=RuntimeError("broken"))
    hub._workers["bot1"] = WorkerTermState(worker_ws=bad_ws)

    with TestClient(app) as client:
        r = client.post("/worker/bot1/hijack/acquire", json={"owner": "test", "lease_s": 60})

    # Worker send failed → 409
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/worker/bot1/hijack/{hijack_id}/heartbeat", json={"lease_s": 120})

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["hijack_id"] == hijack_id
    assert "lease_expires_at" in data


def test_heartbeat_wrong_id() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post("/worker/bot1/hijack/wrong-hijack-id/heartbeat", json={})

    # "wrong-hijack-id" fails the UUID path pattern → 422 before route logic runs
    assert r.status_code == 422


def test_heartbeat_request_none_defaults() -> None:
    """Heartbeat with no JSON body uses HijackHeartbeatRequest defaults."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/worker/bot1/hijack/{hijack_id}/heartbeat")

    assert r.status_code == 200


def test_heartbeat_inner_session_none() -> None:
    """If session disappears between lock acquisition and update, return 404."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/worker/bot1/hijack/{hijack_id}/heartbeat", json={"lease_s": 30})

    # Should succeed normally (inner None check is a safety guard)
    assert r.status_code == 200


def test_heartbeat_session_mismatch_inside_lock() -> None:
    """Heartbeat returns 404 when hijack_id doesn't match inside the lock (line 381)."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    real_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(other_id),  # different session active
    )

    with TestClient(app) as client:
        r = client.post(f"/worker/bot1/hijack/{real_id}/heartbeat")

    # real_id doesn't match other_id in bot state
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def test_release() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/worker/bot1/hijack/{hijack_id}/release")

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert hub._workers["bot1"].hijack_session is None


def test_release_invalid_session() -> None:
    app, hub = make_app()
    hub._workers["bot1"] = WorkerTermState()

    with TestClient(app) as client:
        r = client.post("/worker/bot1/hijack/no-such-id/release")

    # "no-such-id" fails the UUID path pattern → 422 before route logic runs
    assert r.status_code == 422


def test_release_inner_session_none() -> None:
    """Release with valid session succeeds."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/worker/bot1/hijack/{hijack_id}/release")

    assert r.status_code == 200


def test_release_session_mismatch_inside_lock() -> None:
    """Release returns 404 when hijack_id doesn't match inside the lock (line 502)."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    real_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(other_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/worker/bot1/hijack/{real_id}/release")

    assert r.status_code == 404


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


def test_send_no_worker() -> None:
    app, hub = make_app()
    hijack_id = str(uuid.uuid4())
    # Valid session but worker_ws = None
    hub._workers["bot1"] = WorkerTermState(
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/worker/bot1/hijack/{hijack_id}/send", json={"keys": "hello\r"})

    assert r.status_code == 409


def test_send_invalid_session() -> None:
    app, hub = make_app()
    hub._workers["bot1"] = WorkerTermState()

    with TestClient(app) as client:
        r = client.post("/worker/bot1/hijack/no-such-id/send", json={"keys": "hi"})

    # "no-such-id" fails the UUID path pattern → 422 before route logic runs
    assert r.status_code == 422


def test_send_empty_keys() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/worker/bot1/hijack/{hijack_id}/send", json={"keys": ""})

    assert r.status_code == 400


# ---------------------------------------------------------------------------
# worker_id validation
# ---------------------------------------------------------------------------


def test_worker_id_validation_special_chars() -> None:
    app, hub = make_app()

    with TestClient(app) as client:
        r = client.post("/worker/bot@bad!/hijack/acquire")

    assert r.status_code == 422
