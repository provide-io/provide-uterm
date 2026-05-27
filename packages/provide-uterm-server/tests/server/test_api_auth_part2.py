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

from provide.uterm.server import create_server_app, default_server_config

from .test_api_auth_part1 import _two_principal_jwt_app

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

    from provide.uterm.server.models import AuthConfig

    key = "uterm-test-secret-32-byte-minimum-key"
    now = int(time.time())

    viewer_token = _jwt.encode(
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
    worker_token = _jwt.encode(
        {
            "sub": "worker",
            "roles": ["admin"],
            "iss": "provide-uterm",
            "aud": "provide-uterm-server",
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


def test_tunnel_revoke_missing_session_is_idempotent() -> None:
    """Revoking a non-existent tunnel is idempotent (200) — tokens are already gone."""
    client, alice, _ = _two_principal_jwt_app()
    with client:
        r = client.delete(
            "/api/tunnels/tunnel-nonexistent/tokens",
            headers={"Authorization": f"Bearer {alice}"},
        )
        assert r.status_code == 200


def test_tunnel_rotate_session_exists_but_no_tokens_404() -> None:
    """Rotate on a session whose tunnel_tokens entry was dropped → 404.

    Covers the ``tunnel_tokens.get(tunnel_id) is None`` branch in rotate
    (distinct from the ``session is None`` branch which lives earlier).
    """
    client, alice, _ = _two_principal_jwt_app()
    with client:
        r = client.post(
            "/api/tunnels",
            json={"tunnel_type": "terminal"},
            headers={"Authorization": f"Bearer {alice}"},
        )
        tunnel_id = r.json()["tunnel_id"]
        # Simulate the token map drifting from the registry (expiry sweep, etc.)
        client.app.state.uterm_tunnel_tokens.pop(tunnel_id, None)
        r = client.post(
            f"/api/tunnels/{tunnel_id}/tokens/rotate",
            headers={"Authorization": f"Bearer {alice}"},
        )
        assert r.status_code == 404


def test_session_delete_revokes_tunnel_tokens() -> None:
    """Deleting a session must also evict its tunnel_tokens entry.

    Without this cleanup an old share_token could authorize a replacement
    session later created under the same session_id.
    """
    import time

    import jwt as _jwt

    from provide.uterm.server.models import AuthConfig

    key = "uterm-test-secret-32-byte-minimum-key"
    now = int(time.time())
    # Admin alice has session.control.create AND session.control.delete.
    alice_admin = _jwt.encode(
        {
            "sub": "alice",
            "roles": ["admin"],
            "iss": "provide-uterm",
            "aud": "provide-uterm-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=key,
        algorithm="HS256",
    )
    worker = _jwt.encode(
        {
            "sub": "worker",
            "roles": ["admin"],
            "iss": "provide-uterm",
            "aud": "provide-uterm-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=key,
        algorithm="HS256",
    )
    config = default_server_config()
    config.auth = AuthConfig(mode="jwt", jwt_public_key_pem=key, jwt_algorithms=["HS256"], worker_bearer_token=worker)
    app = create_server_app(config)
    with TestClient(app) as client:
        r = client.post(
            "/api/tunnels",
            json={"tunnel_type": "terminal"},
            headers={"Authorization": f"Bearer {alice_admin}"},
        )
        tunnel_id = r.json()["tunnel_id"]
        assert tunnel_id in client.app.state.uterm_tunnel_tokens
        r = client.delete(
            f"/api/sessions/{tunnel_id}",
            headers={"Authorization": f"Bearer {alice_admin}"},
        )
        assert r.status_code == 200
        # Tokens for that session must be gone — no stale bearer capability
        # can authorize a replacement session created later under the same id.
        assert tunnel_id not in client.app.state.uterm_tunnel_tokens


def test_recording_download_no_config_on_app_state(app_client: TestClient, sid: str) -> None:
    """Recording download returns 404 when uterm_config is absent from app state."""
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
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

    from provide.uterm.server.routes.api import create_api_router

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

    from provide.uterm.server.routes.api import create_api_router

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
