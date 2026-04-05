#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage gap tests for routes/profiles.py — 403 paths, error branches, connect edge cases."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from provide.terminal.server import create_server_app, default_server_config

_TEST_KEY = "uterm-test-secret-32-byte-minimum-key"


def _make_token(sub: str = "user1", roles: list[str] | None = None) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": sub,
            "roles": roles or ["operator"],
            "iss": "provide-terminal",
            "aud": "provide-terminal-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=_TEST_KEY,
        algorithm="HS256",
    )


def _jwt_app(tmp_path: Path) -> Any:
    """Create a JWT-auth FastAPI app with a shared temp profile store directory."""
    from provide.terminal.server.models import AuthConfig

    cfg = default_server_config()
    cfg.auth = AuthConfig(
        mode="jwt",
        jwt_public_key_pem=_TEST_KEY,
        jwt_algorithms=["HS256"],
        jwt_issuer="provide-terminal",
        jwt_audience="provide-terminal-server",
        worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
    )
    cfg.profiles.directory = tmp_path / "profiles"
    return create_server_app(cfg)


@pytest.fixture()
def jwt_app(tmp_path: Path) -> Any:
    return _jwt_app(tmp_path)


@pytest.fixture()
def admin_client(jwt_app: Any) -> TestClient:
    headers = {"Authorization": f"Bearer {_make_token(sub='admin-user', roles=['admin'])}"}
    with TestClient(jwt_app, headers=headers) as client:
        yield client  # type: ignore[misc]


@pytest.fixture()
def operator_client(jwt_app: Any) -> TestClient:
    headers = {"Authorization": f"Bearer {_make_token(sub='operator-user', roles=['operator'])}"}
    with TestClient(jwt_app, headers=headers) as client:
        yield client  # type: ignore[misc]


@pytest.fixture()
def viewer_client(jwt_app: Any) -> TestClient:
    headers = {"Authorization": f"Bearer {_make_token(sub='viewer-user', roles=['viewer'])}"}
    with TestClient(jwt_app, headers=headers) as client:
        yield client  # type: ignore[misc]


def _create_profile(client: TestClient, **kwargs: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "My Server",
        "connector_type": "ssh",
        "host": "example.com",
        **kwargs,
    }
    r = client.post("/api/profiles", json=payload)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


# ── Line 44: _principal raises 500 when principal not resolved ──────────────


def test_principal_not_resolved_returns_500() -> None:
    """Calling a profiles route with no principal on request.state returns 500."""
    from fastapi import HTTPException

    from provide.terminal.server.routes.profiles import _principal

    req = SimpleNamespace(state=SimpleNamespace())  # no uterm_principal attribute
    with pytest.raises(HTTPException) as exc_info:
        _principal(req)  # type: ignore[arg-type]
    assert exc_info.value.status_code == 500


# ── Line 63: Non-admin list filters by owner ────────────────────────────────


def test_list_profiles_non_admin_filters_by_owner(admin_client: TestClient, operator_client: TestClient) -> None:
    """Non-admin operators only see their own profiles, not admin's profiles."""
    # Admin creates a profile
    _create_profile(admin_client)
    # Operator creates their own
    _create_profile(operator_client, name="Operator Server")

    r = operator_client.get("/api/profiles")
    assert r.status_code == 200
    profiles = r.json()
    assert len(profiles) == 1
    assert profiles[0]["owner"] == "operator-user"


# ── Line 75: get_profile 403 when user can't read ───────────────────────────


def test_get_profile_403_when_no_read_permission(admin_client: TestClient, operator_client: TestClient) -> None:
    """Operator cannot GET a private profile owned by another user."""
    profile = _create_profile(admin_client)  # admin creates private profile
    r = operator_client.get(f"/api/profiles/{profile['profile_id']}")
    assert r.status_code == 403


# ── Line 125: update_profile 403 when user can't mutate ─────────────────────


def test_update_profile_403_when_no_mutate_permission(admin_client: TestClient, operator_client: TestClient) -> None:
    """Operator cannot PUT a profile owned by another user."""
    profile = _create_profile(admin_client)
    r = operator_client.put(f"/api/profiles/{profile['profile_id']}", json={"name": "Hacked"})
    assert r.status_code == 403


# ── Lines 130-131: ValidationError on update ────────────────────────────────


def test_update_profile_422_on_invalid_field_value(admin_client: TestClient) -> None:
    """Passing an invalid Literal value to update triggers a 422 ValidationError."""
    profile = _create_profile(admin_client)
    r = admin_client.put(
        f"/api/profiles/{profile['profile_id']}",
        json={"input_mode": "BOGUS_VALUE"},
    )
    assert r.status_code == 422


# ── Line 133: update returns None (race: profile deleted between get+update) ─


def test_update_profile_404_when_store_returns_none(admin_client: TestClient, jwt_app: Any) -> None:
    """If store.update_profile returns None (race condition), the route returns 404."""
    profile = _create_profile(admin_client)
    with patch.object(
        jwt_app.state.uterm_profile_store,
        "update_profile",
        new=AsyncMock(return_value=None),
    ):
        r = admin_client.put(
            f"/api/profiles/{profile['profile_id']}",
            json={"name": "x"},
        )
    assert r.status_code == 404


# ── Line 145: delete_profile 403 when user can't mutate ─────────────────────


def test_delete_profile_403_when_no_mutate_permission(admin_client: TestClient, operator_client: TestClient) -> None:
    """Operator cannot DELETE a profile owned by another user."""
    profile = _create_profile(admin_client)
    r = operator_client.delete(f"/api/profiles/{profile['profile_id']}")
    assert r.status_code == 403


# ── Line 163: connect 403 when user can't read profile ───────────────────────


def test_connect_from_profile_403_when_no_read_permission(
    admin_client: TestClient, operator_client: TestClient
) -> None:
    """Operator cannot connect from a private profile owned by another user."""
    profile = _create_profile(admin_client, connector_type="ushell")
    r = operator_client.post(f"/api/profiles/{profile['profile_id']}/connect", json={})
    assert r.status_code == 403


# ── Line 165: connect 403 when user can't create session (viewer) ────────────


def test_connect_from_profile_403_viewer_cannot_create_session(
    admin_client: TestClient, viewer_client: TestClient, jwt_app: Any
) -> None:
    """Viewer role cannot connect even from a shared profile."""
    # Create a shared profile so the viewer can read it
    profile = _create_profile(admin_client, connector_type="ushell", visibility="shared")
    r = viewer_client.post(f"/api/profiles/{profile['profile_id']}/connect", json={})
    assert r.status_code == 403


# ── Lines 167->169, 170: host is None, port is provided ─────────────────────


def test_connect_from_profile_no_host_with_port(admin_client: TestClient) -> None:
    """Profile without host but with port: host branch skipped, port added to config."""
    # Create a ushell profile (no host), but we patch the profile to have port set
    r = admin_client.post(
        "/api/profiles",
        json={
            "name": "Local Shell",
            "connector_type": "ushell",
            # host intentionally omitted (defaults to None)
            "port": 2222,
        },
    )
    assert r.status_code == 200
    profile = r.json()
    assert profile["port"] == 2222
    assert profile["host"] is None

    # Connect — should succeed (ushell ignores host/port but path is exercised)
    r2 = admin_client.post(f"/api/profiles/{profile['profile_id']}/connect", json={})
    assert r2.status_code == 200


# ── Line 189: recording_enabled=True forwarded to session ───────────────────


def test_connect_from_profile_recording_enabled_set(admin_client: TestClient) -> None:
    """When profile.recording_enabled is True, it is forwarded to the session payload."""
    profile = _create_profile(admin_client, connector_type="ushell", recording_enabled=True)
    assert profile["recording_enabled"] is True
    r = admin_client.post(f"/api/profiles/{profile['profile_id']}/connect", json={})
    assert r.status_code == 200
    data = r.json()
    assert "session_id" in data


# ── Lines 192-193: SessionValidationError → 422 ──────────────────────────────


def test_connect_from_profile_422_on_session_validation_error(admin_client: TestClient, jwt_app: Any) -> None:
    """If registry.create_session raises SessionValidationError, route returns 422."""
    from provide.terminal.server.registry import SessionValidationError

    profile = _create_profile(admin_client, connector_type="ushell")
    with patch.object(
        jwt_app.state.uterm_registry,
        "create_session",
        new=AsyncMock(side_effect=SessionValidationError("bad session input")),
    ):
        r = admin_client.post(f"/api/profiles/{profile['profile_id']}/connect", json={})
    assert r.status_code == 422
    assert "bad session input" in r.json()["detail"]


# ── Lines 194-195: ValueError → 409 ─────────────────────────────────────────


def test_connect_from_profile_409_on_conflict(admin_client: TestClient, jwt_app: Any) -> None:
    """If registry.create_session raises ValueError (e.g. duplicate), route returns 409."""
    profile = _create_profile(admin_client, connector_type="ushell")
    with patch.object(
        jwt_app.state.uterm_registry,
        "create_session",
        new=AsyncMock(side_effect=ValueError("session already exists")),
    ):
        r = admin_client.post(f"/api/profiles/{profile['profile_id']}/connect", json={})
    assert r.status_code == 409
    assert "session already exists" in r.json()["detail"]
