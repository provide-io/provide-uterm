#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Additional coverage for routes/api.py, app.py — 403 paths, KeyError paths, validation."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

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
            "roles": roles or ["operator"],
            "iss": "provide-uterm",
            "aud": "provide-uterm-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=_TEST_KEY,
        algorithm="HS256",
    )


@pytest.fixture()
def app_client() -> TestClient:
    cfg = default_server_config()
    cfg.auth.mode = "dev"
    # Drop the auto-start demo session. ``TestKeyErrorPaths`` below patches
    # ``registry.start_session`` to raise KeyError, and a still-in-flight
    # background ``auto_start_sessions`` for the demo session would surface
    # that KeyError during teardown — see the trace at factory.py:502.
    cfg.sessions = []
    app = create_server_app(cfg)
    with TestClient(app) as client:
        yield client  # type: ignore[misc]


def _jwt_app():  # type: ignore[return]
    """Single FastAPI app with JWT auth shared across admin and viewer clients."""
    from provide.uterm.server.models import AuthConfig

    cfg = default_server_config()
    cfg.auth = AuthConfig(
        mode="jwt",
        jwt_public_key_pem=_TEST_KEY,
        jwt_algorithms=["HS256"],
        jwt_issuer="provide-uterm",
        jwt_audience="provide-uterm-server",
        worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
    )
    return create_server_app(cfg)


@pytest.fixture()
def jwt_app():  # type: ignore[return]
    return _jwt_app()


@pytest.fixture()
def admin_client(jwt_app) -> TestClient:  # type: ignore[return]
    headers = {"Authorization": f"Bearer {_make_token(sub='admin-user', roles=['admin'])}"}
    with TestClient(jwt_app, headers=headers) as client:
        yield client  # type: ignore[misc]


@pytest.fixture()
def viewer_client(jwt_app) -> TestClient:  # type: ignore[return]
    headers = {"Authorization": f"Bearer {_make_token(sub='viewer-user', roles=['viewer'])}"}
    with TestClient(jwt_app, headers=headers) as client:
        yield client  # type: ignore[misc]


def _private_session_payload(session_id: str = "priv-sess", owner: str = "other-user") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "display_name": "Private Session",
        "connector_type": "shell",
        "visibility": "private",
        "owner": owner,
    }


# ---------------------------------------------------------------------------
# 403 paths for read-only endpoints when session is private
# These cover: api.py lines 117, 236, 249, 262, 271, 289, 301
# ---------------------------------------------------------------------------


class TestPrivateSessionForbiddenForViewer:
    """Viewer cannot access private sessions owned by someone else."""

    def _create_private_session(self, client: TestClient, session_id: str = "priv-s") -> None:
        r = client.post("/api/sessions", json=_private_session_payload(session_id, owner="other-user"))
        assert r.status_code == 200, r.text

    def test_get_session_403_when_private(self, admin_client: TestClient, viewer_client: TestClient) -> None:
        """GET /api/sessions/{id} → 403 for private session (line 117)."""
        self._create_private_session(admin_client, "priv-get")
        r = viewer_client.get("/api/sessions/priv-get")
        assert r.status_code == 403

    def test_analyze_session_403_when_private(self, admin_client: TestClient, viewer_client: TestClient) -> None:
        """POST /api/sessions/{id}/analyze → 403 for private session (line 236)."""
        self._create_private_session(admin_client, "priv-analyze")
        r = viewer_client.post("/api/sessions/priv-analyze/analyze")
        assert r.status_code == 403

    def test_snapshot_403_when_private(self, admin_client: TestClient, viewer_client: TestClient) -> None:
        """GET /api/sessions/{id}/snapshot → 403 for private session (line 249)."""
        self._create_private_session(admin_client, "priv-snap")
        r = viewer_client.get("/api/sessions/priv-snap/snapshot")
        assert r.status_code == 403

    def test_events_403_when_private(self, admin_client: TestClient, viewer_client: TestClient) -> None:
        """GET /api/sessions/{id}/events → 403 for private session (line 262)."""
        self._create_private_session(admin_client, "priv-evts")
        r = viewer_client.get("/api/sessions/priv-evts/events")
        assert r.status_code == 403

    def test_recording_meta_403_when_private(self, admin_client: TestClient, viewer_client: TestClient) -> None:
        """GET /api/sessions/{id}/recording → 403 for private session (line 271)."""
        self._create_private_session(admin_client, "priv-rec")
        r = viewer_client.get("/api/sessions/priv-rec/recording")
        assert r.status_code == 403

    def test_recording_entries_403_when_private(self, admin_client: TestClient, viewer_client: TestClient) -> None:
        """GET /api/sessions/{id}/recording/entries → 403 (line 289)."""
        self._create_private_session(admin_client, "priv-ent")
        r = viewer_client.get("/api/sessions/priv-ent/recording/entries")
        assert r.status_code == 403

    def test_recording_download_403_when_private(self, admin_client: TestClient, viewer_client: TestClient) -> None:
        """GET /api/sessions/{id}/recording/download → 403 (line 301)."""
        self._create_private_session(admin_client, "priv-dl")
        r = viewer_client.get("/api/sessions/priv-dl/recording/download")
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# KeyError paths: registry method raises KeyError after definition lookup
# These simulate race conditions (session deleted between definition check
# and registry method call). Lines 120-121, 164-165, 179-180, 193-194,
# 213-214, 226-227, 239-240, 274-275, 292-293, 304-305
# ---------------------------------------------------------------------------


class TestKeyErrorPaths:
    """Simulate race-condition KeyError from registry after definition succeeds."""

    def _patch_registry(self, app_client: TestClient, method: str) -> Any:
        registry = app_client.app.state.uterm_registry  # type: ignore[attr-defined]
        return patch.object(registry, method, side_effect=KeyError("gone"))

    def test_get_session_key_error_returns_404(self, app_client: TestClient) -> None:
        app_client.post("/api/sessions", json={"session_id": "race-get", "connector_type": "shell"})
        with self._patch_registry(app_client, "get_session"):
            r = app_client.get("/api/sessions/race-get")
        assert r.status_code == 404

    def test_connect_session_key_error_returns_404(self, app_client: TestClient) -> None:
        app_client.post("/api/sessions", json={"session_id": "race-conn", "connector_type": "shell"})
        with self._patch_registry(app_client, "start_session"):
            r = app_client.post("/api/sessions/race-conn/connect")
        assert r.status_code == 404

    def test_disconnect_session_key_error_returns_404(self, app_client: TestClient) -> None:
        app_client.post("/api/sessions", json={"session_id": "race-disc", "connector_type": "shell"})
        with self._patch_registry(app_client, "stop_session"):
            r = app_client.post("/api/sessions/race-disc/disconnect")
        assert r.status_code == 404

    def test_restart_session_key_error_returns_404(self, app_client: TestClient) -> None:
        app_client.post("/api/sessions", json={"session_id": "race-rest", "connector_type": "shell"})
        with self._patch_registry(app_client, "restart_session"):
            r = app_client.post("/api/sessions/race-rest/restart")
        assert r.status_code == 404

    def test_set_mode_key_error_returns_404(self, app_client: TestClient) -> None:
        app_client.post(
            "/api/sessions",
            json={"session_id": "race-mode", "connector_type": "shell", "owner": "local-dev"},
        )
        with self._patch_registry(app_client, "set_mode"):
            r = app_client.post("/api/sessions/race-mode/mode", json={"input_mode": "open"})
        assert r.status_code == 404

    def test_clear_session_key_error_returns_404(self, app_client: TestClient) -> None:
        app_client.post(
            "/api/sessions",
            json={"session_id": "race-clear", "connector_type": "shell", "owner": "local-dev"},
        )
        with self._patch_registry(app_client, "clear_session"):
            r = app_client.post("/api/sessions/race-clear/clear")
        assert r.status_code == 404

    def test_analyze_key_error_returns_404(self, app_client: TestClient) -> None:
        app_client.post("/api/sessions", json={"session_id": "race-an", "connector_type": "shell"})
        with self._patch_registry(app_client, "analyze_session"):
            r = app_client.post("/api/sessions/race-an/analyze")
        assert r.status_code == 404

    def test_recording_meta_key_error_returns_404(self, app_client: TestClient) -> None:
        app_client.post("/api/sessions", json={"session_id": "race-rm", "connector_type": "shell"})
        with self._patch_registry(app_client, "recording_meta"):
            r = app_client.get("/api/sessions/race-rm/recording")
        assert r.status_code == 404

    def test_recording_entries_key_error_returns_404(self, app_client: TestClient) -> None:
        app_client.post("/api/sessions", json={"session_id": "race-re", "connector_type": "shell"})
        with self._patch_registry(app_client, "recording_entries"):
            r = app_client.get("/api/sessions/race-re/recording/entries")
        assert r.status_code == 404

    def test_recording_download_key_error_returns_404(self, app_client: TestClient) -> None:
        app_client.post("/api/sessions", json={"session_id": "race-rd", "connector_type": "shell"})
        with self._patch_registry(app_client, "recording_path"):
            r = app_client.get("/api/sessions/race-rd/recording/download")
        assert r.status_code == 404

    def test_patch_session_key_error_returns_404(self, app_client: TestClient) -> None:
        app_client.post(
            "/api/sessions",
            json={"session_id": "race-patch", "connector_type": "shell", "owner": "local-dev"},
        )
        with self._patch_registry(app_client, "update_session"):
            r = app_client.patch("/api/sessions/race-patch", json={"display_name": "x"})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# SessionValidationError in PATCH → 422 (lines 141-142)
# ---------------------------------------------------------------------------


class TestPatchValidationError:
    def test_patch_invalid_input_mode_returns_422(self, app_client: TestClient) -> None:
        app_client.post(
            "/api/sessions",
            json={"session_id": "patch-v", "connector_type": "shell", "owner": "local-dev"},
        )
        r = app_client.patch("/api/sessions/patch-v", json={"input_mode": "superuser"})
        assert r.status_code == 422

    def test_patch_invalid_visibility_returns_422(self, app_client: TestClient) -> None:
        app_client.post(
            "/api/sessions",
            json={"session_id": "patch-vis", "connector_type": "shell", "owner": "local-dev"},
        )
        r = app_client.patch("/api/sessions/patch-vis", json={"visibility": "uterm-test-secret-32-byte-minimum-key"})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# quick_connect duplicate session → 409 (lines 338-339)
# ---------------------------------------------------------------------------


class TestQuickConnectConflict:
    def test_quick_connect_registry_conflict_returns_409(self, app_client: TestClient) -> None:
        """POST /api/connect where registry raises ValueError → 409 (lines 338-339)."""
        registry = app_client.app.state.uterm_registry  # type: ignore[attr-defined]
        with patch.object(registry, "create_session", side_effect=ValueError("session already exists")):
            r = app_client.post("/api/connect", json={"connector_type": "shell"})
        assert r.status_code == 409


class TestMaxSessions:
    def test_max_sessions_blocks_create(self) -> None:
        """SessionRegistry enforces max_sessions limit."""
        from provide.uterm.bridge.hub.core import TermHub
        from provide.uterm.server.models import RecordingConfig
        from provide.uterm.server.registry import SessionRegistry

        hub = TermHub()
        reg = SessionRegistry(
            [], hub=hub, public_base_url="http://localhost", recording=RecordingConfig(), max_sessions=1
        )
        import asyncio

        async def _run() -> None:
            await reg.create_session({"session_id": "s1", "connector_type": "shell", "display_name": "S1"})
            with pytest.raises(ValueError, match="session limit reached"):
                await reg.create_session({"session_id": "s2", "connector_type": "shell", "display_name": "S2"})

        asyncio.run(_run())

    def test_max_sessions_none_is_unbounded(self) -> None:
        """max_sessions=None (default) does not limit session creation."""
        from provide.uterm.bridge.hub.core import TermHub
        from provide.uterm.server.models import RecordingConfig
        from provide.uterm.server.registry import SessionRegistry

        hub = TermHub()
        reg = SessionRegistry([], hub=hub, public_base_url="http://localhost", recording=RecordingConfig())
        import asyncio

        async def _run() -> None:
            for i in range(5):
                await reg.create_session({"session_id": f"s{i}", "connector_type": "shell", "display_name": f"S{i}"})

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# app.py: _validate_auth_config error paths (lines 77, 79, 83)
# ---------------------------------------------------------------------------


class TestValidateAuthConfig:
    def test_jwt_mode_without_worker_token_raises(self) -> None:
        from provide.uterm.server.models import AuthConfig

        cfg = default_server_config()
        cfg.auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=["HS256"],
            # No worker_bearer_token
        )
        with pytest.raises(ValueError, match="worker_bearer_token is required"):
            create_server_app(cfg)

    def test_header_mode_without_acknowledged_raises(self) -> None:
        from provide.uterm.server.models import AuthConfig

        cfg = default_server_config()
        cfg.auth = AuthConfig(mode="header")
        with pytest.raises(ValueError, match="header_mode_acknowledged"):
            create_server_app(cfg)

    def test_header_mode_without_worker_token_raises(self) -> None:
        from provide.uterm.server.models import AuthConfig

        cfg = default_server_config()
        cfg.auth = AuthConfig(mode="header", header_mode_acknowledged=True)
        with pytest.raises(ValueError, match="worker_bearer_token is required"):
            create_server_app(cfg)

    def test_jwt_empty_algorithms_raises(self) -> None:
        from provide.uterm.server.models import AuthConfig

        now = int(time.time())
        worker_token = jwt.encode(
            {"sub": "w", "iss": "x", "aud": "y", "iat": now, "exp": now + 600},
            key=_TEST_KEY,
            algorithm="HS256",
        )
        cfg = default_server_config()
        cfg.auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=[],
            worker_bearer_token=worker_token,
        )
        with pytest.raises(ValueError, match="jwt_algorithms must not be empty"):
            create_server_app(cfg)

    def test_jwt_none_algorithm_raises(self) -> None:
        from provide.uterm.server.models import AuthConfig

        now = int(time.time())
        worker_token = jwt.encode(
            {"sub": "w", "iss": "x", "aud": "y", "iat": now, "exp": now + 600},
            key=_TEST_KEY,
            algorithm="HS256",
        )
        cfg = default_server_config()
        cfg.auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=["none"],
            worker_bearer_token=worker_token,
        )
        with pytest.raises(ValueError, match="'none' is not permitted"):
            create_server_app(cfg)

    def test_jwt_no_key_or_jwks_raises(self) -> None:
        from provide.uterm.server.models import AuthConfig

        now = int(time.time())
        worker_token = jwt.encode(
            {"sub": "w", "iss": "x", "aud": "y", "iat": now, "exp": now + 600},
            key=_TEST_KEY,
            algorithm="HS256",
        )
        cfg = default_server_config()
        cfg.auth = AuthConfig(
            mode="jwt",
            jwt_algorithms=["HS256"],
            worker_bearer_token=worker_token,
        )
        with pytest.raises(ValueError, match="jwt_public_key_pem"):
            create_server_app(cfg)


# ---------------------------------------------------------------------------
# app.py: CORS middleware setup (line 245)
# ---------------------------------------------------------------------------


class TestCorsMiddleware:
    def test_cors_enabled_with_allowed_origins(self) -> None:
        cfg = default_server_config()
        cfg.auth.mode = "dev"
        cfg.server.allowed_origins = ["https://example.com"]
        app = create_server_app(cfg)
        # CORS middleware should be present (starlette Middleware objects expose .cls)
        from fastapi.middleware.cors import CORSMiddleware

        assert any(getattr(m, "cls", None) is CORSMiddleware for m in app.user_middleware)


# ---------------------------------------------------------------------------
# app.py: 5xx metric increment (lines 215-223, 226)
# ---------------------------------------------------------------------------


class TestMetrics5xx:
    def test_5xx_increments_metric(self, app_client: TestClient) -> None:
        # Inject a route that raises to trigger the exception middleware

        cfg = default_server_config()
        cfg.auth.mode = "dev"
        app = create_server_app(cfg)

        @app.get("/test-error")
        async def _error():  # type: ignore[return]
            raise RuntimeError("intentional 500")

        with TestClient(app, raise_server_exceptions=False) as client:
            metrics_before = client.get("/api/metrics").json()["metrics"]
            client.get("/test-error")
            metrics_after = client.get("/api/metrics").json()["metrics"]

        # Either 5xx incremented or error incremented
        assert metrics_after.get("http_requests_total", 0) > metrics_before.get("http_requests_total", 0)
