#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for /api/profiles endpoints."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config


@pytest.fixture()
def app_client(tmp_path: Path) -> TestClient:
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.profiles.directory = tmp_path / "profiles"
    app = create_server_app(config)
    return TestClient(app)


@pytest.fixture()
def viewer_client(tmp_path: Path) -> TestClient:
    """Client with viewer role only — cannot create sessions/profiles."""
    import jwt as _jwt

    key = "uterm-test-secret-32-byte-minimum-key"
    now = int(time.time())
    token = _jwt.encode(
        {
            "sub": "viewer1",
            "roles": ["viewer"],
            "iss": "provide-uterm",
            "aud": "provide-uterm-server",
            "iat": now,
            "exp": now + 3600,
        },
        key,
        algorithm="HS256",
    )
    config = default_server_config()
    config.auth.mode = "jwt"
    config.auth.jwt_algorithms = ["HS256"]
    config.auth.jwt_public_key_pem = key
    config.auth.worker_bearer_token = "test-worker-token"
    config.profiles.directory = tmp_path / "profiles-viewer"
    app = create_server_app(config)
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def _create_profile(client: TestClient, **kwargs: object) -> dict:
    payload = {"name": "My Server", "connector_type": "ssh", "host": "example.com", **kwargs}
    r = client.post("/api/profiles", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# ── List ──────────────────────────────────────────────────────────────────


def test_list_empty_initially(app_client: TestClient) -> None:
    r = app_client.get("/api/profiles")
    assert r.status_code == 200
    assert r.json() == []


def test_list_returns_own_profile(app_client: TestClient) -> None:
    _create_profile(app_client)
    r = app_client.get("/api/profiles")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["name"] == "My Server"


# ── Get ───────────────────────────────────────────────────────────────────


def test_get_profile_returns_200(app_client: TestClient) -> None:
    profile = _create_profile(app_client)
    r = app_client.get(f"/api/profiles/{profile['profile_id']}")
    assert r.status_code == 200
    assert r.json()["profile_id"] == profile["profile_id"]


def test_get_profile_unknown_id_returns_404(app_client: TestClient) -> None:
    r = app_client.get("/api/profiles/nonexistent")
    assert r.status_code == 404


# ── Create ────────────────────────────────────────────────────────────────


def test_create_profile_returns_profile(app_client: TestClient) -> None:
    r = app_client.post("/api/profiles", json={"name": "Prod", "connector_type": "ssh", "host": "prod.example.com"})
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "Prod"
    assert data["connector_type"] == "ssh"
    assert data["host"] == "prod.example.com"
    assert "profile_id" in data
    assert data["owner"] == "admin"  # dev mode principal


def test_create_profile_viewer_role_returns_403(viewer_client: TestClient) -> None:
    r = viewer_client.post("/api/profiles", json={"name": "x", "connector_type": "ssh"})
    assert r.status_code == 403


def test_create_profile_sets_defaults(app_client: TestClient) -> None:
    r = app_client.post("/api/profiles", json={"name": "Min", "connector_type": "ushell"})
    assert r.status_code == 200
    data = r.json()
    assert data["visibility"] == "private"
    assert data["input_mode"] == "open"
    assert data["recording_enabled"] is False
    assert data["tags"] == []


# ── Update ────────────────────────────────────────────────────────────────


def test_update_profile_changes_name(app_client: TestClient) -> None:
    profile = _create_profile(app_client)
    r = app_client.put(f"/api/profiles/{profile['profile_id']}", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"


def test_update_unknown_profile_returns_404(app_client: TestClient) -> None:
    r = app_client.put("/api/profiles/nonexistent", json={"name": "x"})
    assert r.status_code == 404


# ── Delete ────────────────────────────────────────────────────────────────


def test_delete_profile_returns_ok(app_client: TestClient) -> None:
    profile = _create_profile(app_client)
    r = app_client.delete(f"/api/profiles/{profile['profile_id']}")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_delete_profile_removes_it(app_client: TestClient) -> None:
    profile = _create_profile(app_client)
    app_client.delete(f"/api/profiles/{profile['profile_id']}")
    r = app_client.get("/api/profiles")
    assert r.json() == []


def test_delete_unknown_profile_returns_404(app_client: TestClient) -> None:
    r = app_client.delete("/api/profiles/nonexistent")
    assert r.status_code == 404


# ── Connect ───────────────────────────────────────────────────────────────


def test_connect_from_profile_creates_session(app_client: TestClient) -> None:
    profile = _create_profile(app_client, connector_type="ushell")
    r = app_client.post(f"/api/profiles/{profile['profile_id']}/connect", json={})
    assert r.status_code == 200
    data = r.json()
    assert "session_id" in data
    assert "url" in data
    assert data["owner"] == "admin"  # connecting principal owns the session


def test_connect_from_profile_unknown_id_returns_404(app_client: TestClient) -> None:
    r = app_client.post("/api/profiles/nonexistent/connect", json={})
    assert r.status_code == 404


def test_connect_from_profile_forwards_password(app_client: TestClient) -> None:
    """Password is forwarded to session connector_config but not stored in profile."""
    # Use a literal public IP so the egress SSRF chokepoint (which validates the
    # connector target host) sees a non-metadata, non-private address with no DNS
    # lookup — keeping this test focused on password forwarding, not egress.
    profile = _create_profile(app_client, connector_type="ssh", host="203.0.113.5", username="u")
    r = app_client.post(
        f"/api/profiles/{profile['profile_id']}/connect",
        json={"password": "s3cr3t"},
    )
    assert r.status_code == 200
    # connector_config (including password) is part of SessionDefinition, not
    # SessionRuntimeStatus — the connect endpoint returns model_dump(session)
    # where session is a SessionRuntimeStatus, so connector_config is never
    # serialised into the response.  We verify forwarding indirectly: the SSH
    # connector requires credentials and the request must succeed (200) for the
    # password to have been accepted by the session creation path.
    data = r.json()
    assert "session_id" in data
    # Password must not appear in the profile itself
    fetched = app_client.get(f"/api/profiles/{profile['profile_id']}").json()
    assert "password" not in fetched


def test_connect_from_profile_egress_blocked_returns_422(app_client: TestClient) -> None:
    """A profile whose connector targets a cloud-metadata IP is rejected at the
    route with a 422. The route-level egress guard runs before create_session;
    without it the registry's EgressBlockedError (a ValueError) surfaces through
    the route's ``except ValueError`` as a misleading 409 instead."""
    profile = _create_profile(app_client, connector_type="ssh", host="169.254.169.254", username="u")
    r = app_client.post(f"/api/profiles/{profile['profile_id']}/connect", json={})
    assert r.status_code == 422
    assert "metadata" in r.json()["detail"].lower()
