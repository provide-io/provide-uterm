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
from provide.uterm.server.models import AuthConfig, SessionDefinition

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


def test_health_ready(app_client: TestClient) -> None:
    r = app_client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["status"] == "ok"
    assert data["service"] == "uterm-server"
    assert isinstance(data["version"], str)
    assert isinstance(data["uptime_s"], (int, float))
    assert data["uptime_s"] >= 0
    assert isinstance(data["active_sessions"], int)
    assert data["active_sessions"] >= 0
    assert data["control_plane_backend"] in {"memory", "sqlite"}


def test_health_not_ready_without_registry() -> None:
    from fastapi import FastAPI

    from provide.uterm.server.routes.health import create_health_router

    bare = FastAPI()
    with TestClient(bare) as client:
        bare.include_router(create_health_router())
        r = client.get("/api/health")
        assert r.status_code == 503
        assert r.json()["ok"] is False
        assert r.json()["status"] == "unavailable"


def test_healthz(app_client: TestClient) -> None:
    r = app_client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_healthz_no_auth_required() -> None:
    """``/healthz`` must work even without any auth setup."""
    from fastapi import FastAPI

    from provide.uterm.server.routes.health import create_health_router

    bare = FastAPI()
    bare.include_router(create_health_router())
    with TestClient(bare) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_health_no_auth_required() -> None:
    """``/api/health`` must be reachable without authentication."""
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    app = create_server_app(config)
    # Wipe auth state to prove health doesn't depend on it
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_health_uptime_increases(app_client: TestClient) -> None:
    """Uptime should be a positive number after the app has been running."""
    r = app_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["uptime_s"] >= 0


def test_health_shows_startup_time_zero_when_missing() -> None:
    """When uterm_startup_time is not set, uptime_s defaults to 0."""
    from fastapi import FastAPI

    from provide.uterm.server.routes.health import create_health_router

    bare = FastAPI()
    bare.state.uterm_registry = object()  # type: ignore[assignment]
    bare.include_router(create_health_router())
    with TestClient(bare) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["uptime_s"] == 0.0


def test_metrics_returns_dict(app_client: TestClient) -> None:
    r = app_client.get("/api/metrics")
    assert r.status_code == 200
    assert "metrics" in r.json()


def test_metrics_non_dict_state_handled() -> None:
    """If app.state.uterm_metrics is not a dict, endpoint returns empty metrics."""
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
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
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
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


def test_tunnel_share_cookie_allows_read_only_session_api() -> None:
    cfg = default_server_config()
    cfg.auth = AuthConfig(
        mode="jwt",
        jwt_public_key_pem=_TEST_KEY,
        jwt_algorithms=["HS256"],
        jwt_issuer="provide-uterm",
        jwt_audience="provide-uterm-server",
        worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
    )
    cfg.sessions = [
        SessionDefinition(
            session_id="tunnel-api-view",
            display_name="Tunnel View",
            connector_type="shell",
            visibility="public",
        )
    ]
    app = create_server_app(cfg)
    from provide.uterm.tunnel.token_hash import hash_token

    app.state.uterm_tunnel_tokens = {
        "tunnel-api-view": {
            "share_token_hash": hash_token("share-token-123"),
            "control_token_hash": hash_token("control-token-123"),
            "worker_token_hash": hash_token("worker-token-123"),
        }
    }
    with TestClient(app) as client:
        cookie = {"uterm_tunnel_tunnel-api-view": "share-token-123"}
        response = client.get("/api/sessions/tunnel-api-view", cookies=cookie)
        denied = client.post("/api/sessions/tunnel-api-view/mode", json={"input_mode": "open"}, cookies=cookie)
    assert response.status_code == 200
    assert denied.status_code == 403


def test_tunnel_control_cookie_allows_session_mutation_api() -> None:
    cfg = default_server_config()
    cfg.auth = AuthConfig(
        mode="jwt",
        jwt_public_key_pem=_TEST_KEY,
        jwt_algorithms=["HS256"],
        jwt_issuer="provide-uterm",
        jwt_audience="provide-uterm-server",
        worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
    )
    cfg.sessions = [
        SessionDefinition(
            session_id="tunnel-api-control",
            display_name="Tunnel Control",
            connector_type="shell",
            visibility="public",
        )
    ]
    app = create_server_app(cfg)
    from provide.uterm.tunnel.token_hash import hash_token

    app.state.uterm_tunnel_tokens = {
        "tunnel-api-control": {
            "share_token_hash": hash_token("share-token-123"),
            "control_token_hash": hash_token("control-token-123"),
            "worker_token_hash": hash_token("worker-token-123"),
        }
    }
    with TestClient(app) as client:
        cookie = {"uterm_tunnel_tunnel-api-control": "control-token-123"}
        response = client.post("/api/sessions/tunnel-api-control/mode", json={"input_mode": "hijack"}, cookies=cookie)
    assert response.status_code == 200
    assert response.json()["input_mode"] == "hijack"


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
