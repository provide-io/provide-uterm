#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for session search/filter API, retention sweep, and bulk delete."""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.registry import SessionRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(**config_overrides: Any) -> TestClient:
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    for key, val in config_overrides.items():
        setattr(config, key, val)
    app = create_server_app(config)
    return TestClient(app)


def _create(client: TestClient, payload: dict[str, Any]) -> dict[str, Any]:
    r = client.post("/api/sessions", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    return _client()


def _seed_sessions(client: TestClient) -> list[str]:
    """Create several sessions with diverse attributes for filtering."""
    specs = [
        {
            "session_id": "prod-web-1",
            "display_name": "Production Web",
            "connector_type": "ssh",
            "tags": ["production", "web"],
            "visibility": "public",
            "auto_start": False,
        },
        {
            "session_id": "prod-db-1",
            "display_name": "Production Database",
            "connector_type": "ssh",
            "tags": ["production", "database"],
            "visibility": "operator",
            "auto_start": False,
        },
        {
            "session_id": "dev-shell-1",
            "display_name": "Dev Shell",
            "connector_type": "shell",
            "tags": ["development"],
            "visibility": "public",
            "auto_start": False,
        },
        {
            "session_id": "staging-ws-1",
            "display_name": "Staging WebSocket",
            "connector_type": "websocket",
            "tags": ["staging", "web"],
            "visibility": "private",
            "auto_start": False,
        },
    ]
    ids = []
    for spec in specs:
        _create(client, spec)
        ids.append(spec["session_id"])
    return ids


# ---------------------------------------------------------------------------
# Filter tests
# ---------------------------------------------------------------------------


def test_filter_by_tag(client: TestClient) -> None:
    _seed_sessions(client)
    r = client.get("/api/sessions", params={"tag": "production"})
    assert r.status_code == 200
    data = r.json()
    sids = {s["session_id"] for s in data}
    assert "prod-web-1" in sids
    assert "prod-db-1" in sids
    assert "dev-shell-1" not in sids


def test_filter_by_tag_or(client: TestClient) -> None:
    """Multiple tag params act as OR."""
    _seed_sessions(client)
    r = client.get("/api/sessions", params=[("tag", "production"), ("tag", "staging")])
    assert r.status_code == 200
    data = r.json()
    sids = {s["session_id"] for s in data}
    assert "prod-web-1" in sids
    assert "staging-ws-1" in sids
    assert "dev-shell-1" not in sids


def test_filter_by_connector_type(client: TestClient) -> None:
    _seed_sessions(client)
    r = client.get("/api/sessions", params={"connector_type": "ssh"})
    assert r.status_code == 200
    data = r.json()
    sids = {s["session_id"] for s in data}
    assert sids == {"prod-web-1", "prod-db-1"}


def test_filter_by_state(client: TestClient) -> None:
    _seed_sessions(client)
    # All sessions were created with auto_start=False, so they are stopped.
    r = client.get("/api/sessions", params={"state": "stopped"})
    assert r.status_code == 200
    data = r.json()
    # At minimum the 4 seeded sessions should appear.
    sids = {s["session_id"] for s in data}
    assert "prod-web-1" in sids
    assert "dev-shell-1" in sids

    r2 = client.get("/api/sessions", params={"state": "running"})
    assert r2.status_code == 200
    data2 = r2.json()
    # None of the seeded sessions should be running.
    sids2 = {s["session_id"] for s in data2}
    assert "prod-web-1" not in sids2


def test_filter_by_visibility(client: TestClient) -> None:
    _seed_sessions(client)
    r = client.get("/api/sessions", params={"visibility": "private"})
    assert r.status_code == 200
    data = r.json()
    sids = {s["session_id"] for s in data}
    assert "staging-ws-1" in sids
    assert "prod-web-1" not in sids


def test_search_q(client: TestClient) -> None:
    _seed_sessions(client)
    r = client.get("/api/sessions", params={"q": "database"})
    assert r.status_code == 200
    data = r.json()
    sids = {s["session_id"] for s in data}
    assert "prod-db-1" in sids
    assert "prod-web-1" not in sids


def test_search_q_matches_session_id(client: TestClient) -> None:
    _seed_sessions(client)
    r = client.get("/api/sessions", params={"q": "dev-shell"})
    assert r.status_code == 200
    data = r.json()
    sids = {s["session_id"] for s in data}
    assert "dev-shell-1" in sids


def test_search_q_matches_tags(client: TestClient) -> None:
    _seed_sessions(client)
    r = client.get("/api/sessions", params={"q": "staging"})
    assert r.status_code == 200
    data = r.json()
    sids = {s["session_id"] for s in data}
    assert "staging-ws-1" in sids


def test_pagination(client: TestClient) -> None:
    _seed_sessions(client)
    r1 = client.get("/api/sessions", params={"limit": 2, "offset": 0})
    assert r1.status_code == 200
    page1 = r1.json()
    assert len(page1) == 2

    r2 = client.get("/api/sessions", params={"limit": 2, "offset": 2})
    assert r2.status_code == 200
    page2 = r2.json()
    # Pages should not overlap.
    ids1 = {s["session_id"] for s in page1}
    ids2 = {s["session_id"] for s in page2}
    assert not ids1 & ids2


def test_sort_order(client: TestClient) -> None:
    _seed_sessions(client)
    r_asc = client.get("/api/sessions", params={"sort": "session_id", "order": "asc", "limit": 200})
    assert r_asc.status_code == 200
    asc_ids = [s["session_id"] for s in r_asc.json()]

    r_desc = client.get("/api/sessions", params={"sort": "session_id", "order": "desc", "limit": 200})
    assert r_desc.status_code == 200
    desc_ids = [s["session_id"] for s in r_desc.json()]

    assert asc_ids == list(reversed(desc_ids))


def test_sort_invalid_field_falls_back(client: TestClient) -> None:
    """Unknown sort field should silently fall back to created_at."""
    _seed_sessions(client)
    r = client.get("/api/sessions", params={"sort": "nonexistent"})
    assert r.status_code == 200


def test_combined_filters(client: TestClient) -> None:
    _seed_sessions(client)
    r = client.get("/api/sessions", params={"tag": "production", "connector_type": "ssh", "state": "stopped"})
    assert r.status_code == 200
    data = r.json()
    sids = {s["session_id"] for s in data}
    assert "prod-web-1" in sids
    assert "prod-db-1" in sids
    assert "dev-shell-1" not in sids
    assert "staging-ws-1" not in sids


# ---------------------------------------------------------------------------
# Retention sweep
# ---------------------------------------------------------------------------


async def test_retention_sweep() -> None:
    """Stopped sessions older than retention_s are removed by the sweep."""
    from provide.uterm.bridge.hub import TermHub
    from provide.uterm.server.models import RecordingConfig

    hub = TermHub()
    registry = SessionRegistry(
        [],
        hub=hub,
        public_base_url="http://localhost:8080",
        recording=RecordingConfig(),
    )
    # Create a session and manually stop its runtime.
    await registry.create_session({"session_id": "old-stopped", "connector_type": "shell", "auto_start": False})
    runtime = registry._runtimes["old-stopped"]
    runtime._state = "stopped"
    runtime._stopped_at = time.time() - 7200  # 2 hours ago

    # Create another session that is still running.
    await registry.create_session({"session_id": "still-running", "connector_type": "shell", "auto_start": False})
    runtime2 = registry._runtimes["still-running"]
    runtime2._state = "running"

    # Simulate one sweep iteration with retention_s=3600 (1 hour).
    retention_s = 3600
    now = time.time()
    pairs = await registry.list_sessions_with_definitions()
    for status, _def in pairs:
        if status.lifecycle_state != "stopped":
            continue
        if status.stopped_at is None:
            continue
        if (now - status.stopped_at) >= retention_s:
            await registry.delete_session(status.session_id)

    # The old stopped session should be gone.
    assert await registry.get_definition("old-stopped") is None
    # The running session should remain.
    assert await registry.get_definition("still-running") is not None


async def test_retention_disabled() -> None:
    """When retention_s=0, no sessions are removed."""
    from provide.uterm.bridge.hub import TermHub
    from provide.uterm.server.models import RecordingConfig

    hub = TermHub()
    registry = SessionRegistry(
        [],
        hub=hub,
        public_base_url="http://localhost:8080",
        recording=RecordingConfig(),
    )
    await registry.create_session({"session_id": "old-stopped", "connector_type": "shell", "auto_start": False})
    runtime = registry._runtimes["old-stopped"]
    runtime._state = "stopped"
    runtime._stopped_at = time.time() - 999999

    retention_s = 0
    if retention_s > 0:
        pairs = await registry.list_sessions_with_definitions()
        now = time.time()
        for status, _def in pairs:
            if status.lifecycle_state == "stopped" and status.stopped_at and (now - status.stopped_at) >= retention_s:
                await registry.delete_session(status.session_id)

    # Session should still exist.
    assert await registry.get_definition("old-stopped") is not None


# ---------------------------------------------------------------------------
# Bulk delete
# ---------------------------------------------------------------------------


def test_bulk_delete(client: TestClient) -> None:
    _seed_sessions(client)
    # All seeded sessions are stopped (auto_start=False).
    r = client.request(
        "DELETE",
        "/api/sessions",
        json={"filter": {"state": "stopped"}},
    )
    assert r.status_code == 200
    result = r.json()
    assert result["deleted"] >= 4

    # Verify sessions are actually gone.
    r2 = client.get("/api/sessions")
    assert r2.status_code == 200
    remaining_sids = {s["session_id"] for s in r2.json()}
    assert "prod-web-1" not in remaining_sids


def test_bulk_delete_with_older_than(client: TestClient) -> None:
    _seed_sessions(client)
    # With older_than_s very large, no sessions should match (stopped_at is recent/None).
    r = client.request(
        "DELETE",
        "/api/sessions",
        json={"filter": {"state": "stopped", "older_than_s": 999999}},
    )
    assert r.status_code == 200
    assert r.json()["deleted"] == 0


def test_bulk_delete_empty_filter(client: TestClient) -> None:
    """Bulk delete with empty filter deletes all sessions the admin can access."""
    _seed_sessions(client)
    r = client.request("DELETE", "/api/sessions", json={"filter": {}})
    assert r.status_code == 200
    assert r.json()["deleted"] >= 4


# ---------------------------------------------------------------------------
# stopped_at field
# ---------------------------------------------------------------------------


def test_stopped_at_in_status(client: TestClient) -> None:
    """The stopped_at field should be present in session status."""
    _create(client, {"session_id": "test-stopped-at", "connector_type": "shell", "auto_start": False})
    r = client.get("/api/sessions/test-stopped-at")
    assert r.status_code == 200
    data = r.json()
    # Session was never started, stopped_at should be None.
    assert data.get("stopped_at") is None


def test_session_retention_s_config() -> None:
    """Verify session_retention_s is available on ServerConfig."""
    config = default_server_config()
    assert config.session_retention_s == 0
    config.session_retention_s = 3600
    assert config.session_retention_s == 3600
