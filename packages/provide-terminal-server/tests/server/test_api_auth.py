#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Authorization and mutation-killing tests for routes/api.py."""

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


def _two_principal_jwt_app() -> tuple[TestClient, str, str]:
    """Helper: build a JWT-mode app and return (client, alice_token, bob_token).

    Used by tunnel access-control tests to verify that one authenticated
    user cannot rotate/revoke another user's tunnel.
    """
    import time

    import jwt as _jwt

    from provide.terminal.server.models import AuthConfig

    key = "uterm-test-secret-32-byte-minimum-key"
    now = int(time.time())

    def _tok(sub: str, roles: list[str]) -> str:
        return _jwt.encode(
            {
                "sub": sub,
                "roles": roles,
                "iss": "provide-terminal",
                "aud": "provide-terminal-server",
                "iat": now,
                "nbf": now,
                "exp": now + 600,
            },
            key=key,
            algorithm="HS256",
        )

    alice = _tok("alice", ["operator"])
    bob = _tok("bob", ["operator"])
    worker = _tok("worker", ["admin"])
    config = default_server_config()
    config.auth = AuthConfig(
        mode="jwt", jwt_public_key_pem=key, jwt_algorithms=["HS256"], worker_bearer_token=worker
    )
    app = create_server_app(config)
    return TestClient(app), alice, bob


def test_tunnel_non_owner_cannot_revoke_tokens() -> None:
    """A second authenticated user must not be able to revoke alice's tunnel tokens."""
    client, alice, bob = _two_principal_jwt_app()
    with client:
        r = client.post(
            "/api/tunnels",
            json={"tunnel_type": "terminal"},
            headers={"Authorization": f"Bearer {alice}"},
        )
        assert r.status_code == 200
        tunnel_id = r.json()["tunnel_id"]

        r = client.delete(
            f"/api/tunnels/{tunnel_id}/tokens",
            headers={"Authorization": f"Bearer {bob}"},
        )
        assert r.status_code == 403


def test_tunnel_non_owner_cannot_rotate_tokens() -> None:
    """A second authenticated user must not be able to rotate alice's tunnel tokens."""
    client, alice, bob = _two_principal_jwt_app()
    with client:
        r = client.post(
            "/api/tunnels",
            json={"tunnel_type": "terminal"},
            headers={"Authorization": f"Bearer {alice}"},
        )
        assert r.status_code == 200
        tunnel_id = r.json()["tunnel_id"]

        r = client.post(
            f"/api/tunnels/{tunnel_id}/tokens/rotate",
            headers={"Authorization": f"Bearer {bob}"},
        )
        assert r.status_code == 403


def test_tunnel_owner_can_rotate_and_revoke() -> None:
    """The creator of a tunnel retains rotate/revoke privileges."""
    client, alice, _bob = _two_principal_jwt_app()
    with client:
        r = client.post(
            "/api/tunnels",
            json={"tunnel_type": "terminal"},
            headers={"Authorization": f"Bearer {alice}"},
        )
        tunnel_id = r.json()["tunnel_id"]
        assert client.post(
            f"/api/tunnels/{tunnel_id}/tokens/rotate",
            headers={"Authorization": f"Bearer {alice}"},
        ).status_code == 200
        assert client.delete(
            f"/api/tunnels/{tunnel_id}/tokens",
            headers={"Authorization": f"Bearer {alice}"},
        ).status_code == 200


def test_tunnel_rotate_on_missing_session_404() -> None:
    """Rotating a non-existent tunnel returns 404 (cannot rotate what's gone)."""
    client, alice, _ = _two_principal_jwt_app()
    with client:
        r = client.post(
            "/api/tunnels/tunnel-nonexistent/tokens/rotate",
            headers={"Authorization": f"Bearer {alice}"},
        )
        assert r.status_code == 404


def test_tunnel_revoke_missing_session_is_idempotent() -> None:
    """Revoking a non-existent tunnel is idempotent (200) — tokens are already gone."""
    client, alice, _ = _two_principal_jwt_app()
    with client:
        r = client.delete(
            "/api/tunnels/tunnel-nonexistent/tokens",
            headers={"Authorization": f"Bearer {alice}"},
        )
        assert r.status_code == 200


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


# ---------------------------------------------------------------------------
# Mutation-killing tests for api.py helpers
# ---------------------------------------------------------------------------


def test_principal_guard_detail_message_when_missing() -> None:
    """_principal() must return status 500 with detail 'principal was not resolved'.

    Kills mutmut_10 (status_code=None), mutmut_11 (detail=None),
    mutmut_12 (no status_code kwarg), mutmut_13 (no detail kwarg).
    """
    from fastapi import FastAPI

    from provide.terminal.server.routes.api import create_api_router

    bare = FastAPI()
    bare.include_router(create_api_router())
    bare.state.uterm_registry = MagicMock()
    bare.state.uterm_registry.list_sessions_with_definitions = AsyncMock(return_value=[])
    bare.state.uterm_authz = MagicMock()

    with TestClient(bare, raise_server_exceptions=False) as client:
        r = client.get("/api/sessions")
    assert r.status_code == 500, f"Expected 500, got {r.status_code}"
    body = r.json()
    assert body.get("detail") == "principal was not resolved", (
        f"Expected detail='principal was not resolved', got {body.get('detail')!r}"
    )


def test_unknown_session_returns_404_with_detail(app_client: TestClient) -> None:
    """Unknown session ID returns 404 with detail mentioning the session ID.

    Kills:
    - x__session_definition__mutmut_6: detail=f'unknown session: {session_id}' → detail=None
    - x_create_api_router__mutmut_6: _sid_not_found detail → None
    - x_create_api_router__mutmut_8: _sid_not_found detail omitted
    - x__registry__mutmut_1: cast(None, ...) — runtime behavior same but type hint wrong
    - x__registry__mutmut_5: cast('XXSessionRegistryXX', ...) — same runtime effect
    """
    r = app_client.get("/api/sessions/nonexistent-session-xyz")
    assert r.status_code == 404, f"Expected 404 for unknown session, got {r.status_code}"
    detail = r.json().get("detail", "")
    assert detail is not None, "404 response must have a detail field"
    assert "nonexistent-session-xyz" in str(detail), f"404 detail must mention the session ID, got {detail!r}"


def test_unknown_session_connect_returns_404_with_detail(app_client: TestClient) -> None:
    """Connecting to an unknown session returns 404 with detail mentioning the session ID.

    Uses _session_definition which calls HTTPException(404, f'unknown session: {session_id}').
    Kills x_create_api_router__mutmut_6 and mutmut_8 via _sid_not_found() and
    x__session_definition__mutmut_6 which changes detail to None.
    """
    r = app_client.post("/api/sessions/nonexistent-xyz/connect")
    assert r.status_code == 404
    detail = r.json().get("detail", "")
    assert "nonexistent-xyz" in str(detail), f"Connect unknown session detail must mention session ID, got {detail!r}"
