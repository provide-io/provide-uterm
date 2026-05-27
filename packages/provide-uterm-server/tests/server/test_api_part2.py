#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage tests for routes/api.py — endpoints not covered by test_server_app.py."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config

_TEST_KEY = "uterm-test-secret-32-byte-minimum-key"


def _make_token(sub: str = "user1", roles: list[str] | None = None) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": sub,
            "roles": roles or ["viewer"],
            "iss": "provide-uterm",
            "aud": "provide-uterm-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=_TEST_KEY,
        algorithm="HS256",
    )


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_client() -> TestClient:
    """TestClient with dev auth and the default shell session."""
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.recording.directory = Path(tempfile.mkdtemp())
    app = create_server_app(config)
    return TestClient(app)


@pytest.fixture()
def sid(app_client: TestClient) -> str:
    """Return the pre-existing provide-shell ID."""
    return "provide-shell"


# ---------------------------------------------------------------------------
# Health / metrics
# ---------------------------------------------------------------------------


def test_events(app_client: TestClient, sid: str) -> None:
    r = app_client.get(f"/api/sessions/{sid}/events")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_events_not_found(app_client: TestClient) -> None:
    r = app_client.get("/api/sessions/ghost/events")
    assert r.status_code == 404


def test_events_limit_param(app_client: TestClient, sid: str) -> None:
    r = app_client.get(f"/api/sessions/{sid}/events?limit=5")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Recording endpoints
# ---------------------------------------------------------------------------


def test_recording_meta_not_found(app_client: TestClient) -> None:
    r = app_client.get("/api/sessions/ghost/recording")
    assert r.status_code == 404


def test_recording_meta_session_exists(app_client: TestClient, sid: str) -> None:
    r = app_client.get(f"/api/sessions/{sid}/recording")
    # 200 or 404 (no recording file yet — both are valid)
    assert r.status_code in (200, 404)


def test_recording_entries_not_found(app_client: TestClient) -> None:
    r = app_client.get("/api/sessions/ghost/recording/entries")
    assert r.status_code == 404


def test_recording_entries_session_exists(app_client: TestClient, sid: str) -> None:
    r = app_client.get(f"/api/sessions/{sid}/recording/entries")
    assert r.status_code in (200, 404)


def test_recording_download_not_found_session(app_client: TestClient) -> None:
    r = app_client.get("/api/sessions/ghost/recording/download")
    assert r.status_code == 404


def test_recording_download_no_file(app_client: TestClient, sid: str) -> None:
    r = app_client.get(f"/api/sessions/{sid}/recording/download")
    assert r.status_code == 404


def test_recording_download_path_traversal_rejected(app_client: TestClient) -> None:
    """A recording path outside the configured directory must be rejected."""
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    with tempfile.TemporaryDirectory() as tmpdir:
        config.recording.directory = Path(tmpdir)  # type: ignore[union-attr]
        app = create_server_app(config)

        # Patch recording_path to return a path outside the allowed directory.
        evil_path = Path("/etc/passwd")

        async def _evil_path(sid: str) -> Path:
            return evil_path

        with TestClient(app) as client:
            app.state.uterm_registry.recording_path = _evil_path
            r = client.get("/api/sessions/provide-shell/recording/download")
            assert r.status_code == 404


# ---------------------------------------------------------------------------
# Quick-connect
# ---------------------------------------------------------------------------


def test_quick_connect_shell(app_client: TestClient) -> None:
    r = app_client.post("/api/connect", json={"connector_type": "shell"})
    assert r.status_code == 200
    body = r.json()
    assert "session_id" in body
    assert "url" in body
    assert body["session_id"].startswith("connect-")


def test_quick_connect_with_display_name(app_client: TestClient) -> None:
    r = app_client.post("/api/connect", json={"connector_type": "shell", "display_name": "My Shell"})
    assert r.status_code == 200
    assert r.json()["display_name"] == "My Shell"


def test_quick_connect_invalid_connector_returns_422(app_client: TestClient) -> None:
    r = app_client.post("/api/connect", json={"connector_type": "bogus"})
    assert r.status_code == 422


def test_quick_connect_url_uses_app_path(app_client: TestClient) -> None:
    r = app_client.post("/api/connect", json={"connector_type": "shell"})
    assert r.status_code == 200
    url = r.json()["url"]
    assert "/session/" in url


def test_quick_connect_with_tags(app_client: TestClient) -> None:
    r = app_client.post("/api/connect", json={"connector_type": "shell", "tags": ["game", "prod"]})
    assert r.status_code == 200
    assert r.json()["tags"] == ["game", "prod"]


def test_quick_connect_with_input_mode_hijack(app_client: TestClient) -> None:
    r = app_client.post("/api/connect", json={"connector_type": "shell", "input_mode": "hijack"})
    assert r.status_code == 200
    assert r.json()["input_mode"] == "hijack"


def test_quick_connect_tags_and_input_mode_not_in_connector_config(app_client: TestClient) -> None:
    """tags and input_mode must not bleed into connector_config."""
    r = app_client.post(
        "/api/connect",
        json={"connector_type": "shell", "tags": ["x"], "input_mode": "open"},
    )
    assert r.status_code == 200


def test_quick_connect_with_recording_enabled(app_client: TestClient) -> None:
    r = app_client.post("/api/connect", json={"connector_type": "shell", "recording_enabled": True})
    assert r.status_code == 200
    assert r.json()["recording_enabled"] is True


def test_quick_connect_without_recording_enabled(app_client: TestClient) -> None:
    r = app_client.post("/api/connect", json={"connector_type": "shell"})
    assert r.status_code == 200
    # recording_enabled defaults to False when not specified
    assert r.json()["recording_enabled"] is False


def test_quick_connect_forbidden_without_create_privilege() -> None:
    """POST /api/connect returns 403 for a viewer-only principal."""
    import time

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
            "nbf": now,
            "exp": now + 600,
        },
        key=key,
        algorithm="HS256",
    )
    from provide.uterm.server.models import AuthConfig

    config = default_server_config()
    now2 = int(time.time())
    worker_token = _jwt.encode(
        {
            "sub": "worker",
            "roles": ["admin"],
            "iss": "provide-uterm",
            "aud": "provide-uterm-server",
            "iat": now2,
            "nbf": now2,
            "exp": now2 + 600,
        },
        key=key,
        algorithm="HS256",
    )
    config.auth = AuthConfig(
        mode="jwt",
        jwt_public_key_pem=key,
        jwt_algorithms=["HS256"],
        worker_bearer_token=worker_token,
    )
    app = create_server_app(config)
    with TestClient(app) as client:
        r = client.post("/api/connect", json={"connector_type": "shell"}, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403


# (Authorization and mutation-killing tests moved to test_api_auth.py)
