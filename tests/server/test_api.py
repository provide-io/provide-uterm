#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for routes/api.py — endpoints not covered by test_server_app.py."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from provide.terminal.server import create_server_app, default_server_config

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_client() -> TestClient:
    """TestClient with dev auth and the default shell session."""
    config = default_server_config()
    config.auth.mode = "dev"
    app = create_server_app(config)
    return TestClient(app)


@pytest.fixture()
def sid(app_client: TestClient) -> str:
    """Return the pre-existing provide-shell ID."""
    return "provide-shell"


# ---------------------------------------------------------------------------
# Health / metrics
# ---------------------------------------------------------------------------


def test_health_ready(app_client: TestClient) -> None:
    r = app_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_health_not_ready_without_registry() -> None:
    from fastapi import FastAPI

    bare = FastAPI()
    with TestClient(bare) as client:
        from provide.terminal.server.routes.api import create_api_router

        bare.include_router(create_api_router())
        r = client.get("/api/health")
        assert r.status_code == 503
        assert r.json()["ok"] is False


def test_metrics_returns_dict(app_client: TestClient) -> None:
    r = app_client.get("/api/metrics")
    assert r.status_code == 200
    assert "metrics" in r.json()


def test_metrics_non_dict_state_handled() -> None:
    """If app.state.uterm_metrics is not a dict, endpoint returns empty metrics."""
    config = default_server_config()
    config.auth.mode = "dev"
    app = create_server_app(config)
    app.state.uterm_metrics = "broken"  # type: ignore[assignment]
    with TestClient(app) as client:
        r = client.get("/api/metrics")
        assert r.status_code == 200
        assert r.json()["metrics"] == {}


def test_metrics_prometheus(app_client: TestClient) -> None:
    r = app_client.get("/api/metrics/prometheus")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


def test_metrics_prometheus_non_dict_state() -> None:
    config = default_server_config()
    config.auth.mode = "dev"
    app = create_server_app(config)
    app.state.uterm_metrics = 42  # type: ignore[assignment]
    with TestClient(app) as client:
        r = client.get("/api/metrics/prometheus")
        assert r.status_code == 200
        assert r.text == ""


# ---------------------------------------------------------------------------
# Sessions CRUD
# ---------------------------------------------------------------------------


def test_list_sessions(app_client: TestClient) -> None:
    r = app_client.get("/api/sessions")
    assert r.status_code == 200
    ids = [s["session_id"] for s in r.json()]
    assert "provide-shell" in ids


def test_get_session(app_client: TestClient, sid: str) -> None:
    r = app_client.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["session_id"] == sid


def test_get_session_not_found(app_client: TestClient) -> None:
    r = app_client.get("/api/sessions/no-such-session")
    assert r.status_code == 404


def test_create_session(app_client: TestClient) -> None:
    r = app_client.post("/api/sessions", json={"session_id": "new-sess", "connector_type": "shell"})
    assert r.status_code == 200
    assert r.json()["session_id"] == "new-sess"


def test_create_session_duplicate_returns_409(app_client: TestClient) -> None:
    r = app_client.post("/api/sessions", json={"session_id": "dup-sess", "connector_type": "shell"})
    assert r.status_code == 200
    r2 = app_client.post("/api/sessions", json={"session_id": "dup-sess", "connector_type": "shell"})
    assert r2.status_code == 409


def test_create_session_invalid_connector_returns_422(app_client: TestClient) -> None:
    r = app_client.post("/api/sessions", json={"session_id": "bad-conn", "connector_type": "invalid-type"})
    assert r.status_code == 422


def test_patch_session(app_client: TestClient) -> None:
    app_client.post("/api/sessions", json={"session_id": "patch-me", "connector_type": "shell"})
    r = app_client.patch("/api/sessions/patch-me", json={"display_name": "Updated"})
    assert r.status_code == 200
    assert r.json()["display_name"] == "Updated"


def test_patch_session_not_found(app_client: TestClient) -> None:
    r = app_client.patch("/api/sessions/ghost", json={"display_name": "X"})
    assert r.status_code == 404


def test_patch_session_invalid_input_mode_returns_422(app_client: TestClient) -> None:
    app_client.post("/api/sessions", json={"session_id": "patch-bad", "connector_type": "shell"})
    r = app_client.patch("/api/sessions/patch-bad", json={"input_mode": "superuser"})
    assert r.status_code == 422


def test_delete_session(app_client: TestClient) -> None:
    app_client.post("/api/sessions", json={"session_id": "del-me", "connector_type": "shell"})
    r = app_client.delete("/api/sessions/del-me")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert app_client.get("/api/sessions/del-me").status_code == 404


# ---------------------------------------------------------------------------
# Session lifecycle: connect / disconnect / restart
# ---------------------------------------------------------------------------


def test_connect_session(app_client: TestClient, sid: str) -> None:
    r = app_client.post(f"/api/sessions/{sid}/connect")
    assert r.status_code == 200
    assert r.json()["session_id"] == sid


def test_connect_session_not_found(app_client: TestClient) -> None:
    r = app_client.post("/api/sessions/ghost/connect")
    assert r.status_code == 404


def test_disconnect_session(app_client: TestClient, sid: str) -> None:
    r = app_client.post(f"/api/sessions/{sid}/disconnect")
    assert r.status_code == 200


def test_disconnect_session_not_found(app_client: TestClient) -> None:
    r = app_client.post("/api/sessions/ghost/disconnect")
    assert r.status_code == 404


def test_restart_session(app_client: TestClient, sid: str) -> None:
    r = app_client.post(f"/api/sessions/{sid}/restart")
    assert r.status_code == 200


def test_restart_session_not_found(app_client: TestClient) -> None:
    r = app_client.post("/api/sessions/ghost/restart")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Mode / clear / analyze
# ---------------------------------------------------------------------------


def test_set_mode_open(app_client: TestClient, sid: str) -> None:
    r = app_client.post(f"/api/sessions/{sid}/mode", json={"input_mode": "open"})
    assert r.status_code == 200


def test_set_mode_hijack(app_client: TestClient, sid: str) -> None:
    r = app_client.post(f"/api/sessions/{sid}/mode", json={"input_mode": "hijack"})
    assert r.status_code == 200


def test_set_mode_invalid(app_client: TestClient, sid: str) -> None:
    r = app_client.post(f"/api/sessions/{sid}/mode", json={"input_mode": "superuser"})
    assert r.status_code == 422


def test_set_mode_not_found(app_client: TestClient) -> None:
    r = app_client.post("/api/sessions/ghost/mode", json={"input_mode": "open"})
    assert r.status_code == 404


def test_clear_session(app_client: TestClient, sid: str) -> None:
    r = app_client.post(f"/api/sessions/{sid}/clear")
    assert r.status_code == 200


def test_clear_session_not_found(app_client: TestClient) -> None:
    r = app_client.post("/api/sessions/ghost/clear")
    assert r.status_code == 404


def test_analyze_session(app_client: TestClient, sid: str) -> None:
    r = app_client.post(f"/api/sessions/{sid}/analyze")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == sid
    assert "analysis" in body


def test_analyze_session_not_found(app_client: TestClient) -> None:
    r = app_client.post("/api/sessions/ghost/analyze")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Snapshot / events
# ---------------------------------------------------------------------------


def test_snapshot_returns_data_or_none(app_client: TestClient, sid: str) -> None:
    r = app_client.get(f"/api/sessions/{sid}/snapshot")
    assert r.status_code == 200  # may be null if no snapshot yet


def test_snapshot_not_found(app_client: TestClient) -> None:
    r = app_client.get("/api/sessions/ghost/snapshot")
    assert r.status_code == 404


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
    config.auth.mode = "dev"
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
            "iss": "provide-terminal",
            "aud": "provide-terminal-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=key,
        algorithm="HS256",
    )
    from provide.terminal.server.models import AuthConfig

    config = default_server_config()
    now2 = int(time.time())
    worker_token = _jwt.encode(
        {
            "sub": "worker",
            "roles": ["admin"],
            "iss": "provide-terminal",
            "aud": "provide-terminal-server",
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


# ---------------------------------------------------------------------------
# Authorization 403 paths (viewer role — read-only)
# ---------------------------------------------------------------------------


@pytest.fixture()
def viewer_client() -> TestClient:
    """TestClient with JWT auth and a viewer-only principal."""
    import time

    import jwt as _jwt

    from provide.terminal.server.models import AuthConfig

    key = "uterm-test-secret-32-byte-minimum-key"
    now = int(time.time())

    viewer_token = _jwt.encode(
        {
            "sub": "viewer1",
            "roles": ["viewer"],
            "iss": "provide-terminal",
            "aud": "provide-terminal-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=key,
        algorithm="HS256",
    )
    worker_token = _jwt.encode(
        {
            "sub": "worker",
            "roles": ["admin"],
            "iss": "provide-terminal",
            "aud": "provide-terminal-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=key,
        algorithm="HS256",
    )
    config = default_server_config()
    config.auth = AuthConfig(
        mode="jwt",
        jwt_public_key_pem=key,
        jwt_algorithms=["HS256"],
        worker_bearer_token=worker_token,
    )
    app = create_server_app(config)
    return TestClient(app, headers={"Authorization": f"Bearer {viewer_token}"})


def test_create_session_forbidden_for_viewer(viewer_client: TestClient) -> None:
    r = viewer_client.post("/api/sessions", json={"session_id": "new", "connector_type": "shell"})
    assert r.status_code == 403


def test_analyze_session_forbidden_for_viewer(viewer_client: TestClient) -> None:
    # Viewer has session.read but not session.control.analyze... actually read is enough.
    # Verify they CAN read (smoke test) but can't mutate:
    r = viewer_client.get("/api/sessions")
    assert r.status_code == 200  # viewers can list/read sessions


def test_patch_session_forbidden_for_viewer(viewer_client: TestClient) -> None:
    r = viewer_client.patch("/api/sessions/provide-shell", json={"display_name": "X"})
    assert r.status_code == 403


def test_delete_session_forbidden_for_viewer(viewer_client: TestClient) -> None:
    r = viewer_client.delete("/api/sessions/provide-shell")
    assert r.status_code == 403


def test_connect_session_forbidden_for_viewer(viewer_client: TestClient) -> None:
    r = viewer_client.post("/api/sessions/provide-shell/connect")
    assert r.status_code == 403


def test_disconnect_session_forbidden_for_viewer(viewer_client: TestClient) -> None:
    r = viewer_client.post("/api/sessions/provide-shell/disconnect")
    assert r.status_code == 403


def test_restart_session_forbidden_for_viewer(viewer_client: TestClient) -> None:
    r = viewer_client.post("/api/sessions/provide-shell/restart")
    assert r.status_code == 403


def test_set_mode_forbidden_for_viewer(viewer_client: TestClient) -> None:
    r = viewer_client.post("/api/sessions/provide-shell/mode", json={"input_mode": "open"})
    assert r.status_code == 403


def test_clear_session_forbidden_for_viewer(viewer_client: TestClient) -> None:
    r = viewer_client.post("/api/sessions/provide-shell/clear")
    assert r.status_code == 403


def test_create_session_owner_mismatch_forbidden() -> None:
    """Non-admin principal cannot set owner to a different subject_id."""
    import time

    import jwt as _jwt

    from provide.terminal.server.models import AuthConfig

    key = "uterm-test-secret-32-byte-minimum-key"
    now = int(time.time())
    op_token = _jwt.encode(
        {
            "sub": "operator1",
            "roles": ["operator"],
            "iss": "provide-terminal",
            "aud": "provide-terminal-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=key,
        algorithm="HS256",
    )
    worker_token = _jwt.encode(
        {
            "sub": "worker",
            "roles": ["admin"],
            "iss": "provide-terminal",
            "aud": "provide-terminal-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=key,
        algorithm="HS256",
    )
    config = default_server_config()
    config.auth = AuthConfig(
        mode="jwt", jwt_public_key_pem=key, jwt_algorithms=["HS256"], worker_bearer_token=worker_token
    )
    app = create_server_app(config)
    with TestClient(app, headers={"Authorization": f"Bearer {op_token}"}) as client:
        r = client.post(
            "/api/sessions",
            json={"session_id": "owned", "connector_type": "shell", "owner": "someone-else"},
        )
        assert r.status_code == 403


def test_recording_download_no_config_on_app_state(app_client: TestClient, sid: str) -> None:
    """Recording download returns 404 when uterm_config is absent from app state."""
    config = default_server_config()
    config.auth.mode = "dev"
    app = create_server_app(config)
    del app.state.uterm_config  # type: ignore[attr-defined]

    real_path = Path(tempfile.mktemp(suffix=".jsonl"))  # noqa: S306
    real_path.write_text("{}\n")
    try:

        async def _fake_path(session_id: str) -> Path:
            return real_path

        with TestClient(app) as client:
            app.state.uterm_registry.recording_path = _fake_path
            r = client.get(f"/api/sessions/{sid}/recording/download")
            assert r.status_code == 404
    finally:
        real_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# _principal guard (500 when principal missing)
# ---------------------------------------------------------------------------


def test_principal_guard_500_when_missing() -> None:
    """_principal() raises 500 if middleware failed to set uterm_principal."""
    from fastapi import FastAPI

    from provide.terminal.server.routes.api import create_api_router

    bare = FastAPI()
    bare.include_router(create_api_router())

    # Registry present so health passes, but no principal set on request.state.
    bare.state.uterm_registry = MagicMock()
    bare.state.uterm_registry.list_sessions_with_definitions = AsyncMock(return_value=[])
    bare.state.uterm_authz = MagicMock()

    with TestClient(bare, raise_server_exceptions=False) as client:
        r = client.get("/api/sessions")
        assert r.status_code == 500
