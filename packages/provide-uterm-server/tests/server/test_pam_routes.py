# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI parity tests for the shared PAM event ingestion route."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from provide.uterm.server.registry import SessionValidationError
from provide.uterm.server.routes.api import create_api_router


def _app_for(principal: object | None, registry: MagicMock, *, can_create_session: bool = True) -> FastAPI:
    app = FastAPI()
    app.state.uterm_registry = registry
    app.state.uterm_authz = MagicMock()
    app.state.uterm_authz.is_admin = AsyncMock(
        side_effect=lambda value: "admin" in getattr(value, "roles", frozenset())
    )
    app.state.uterm_authz.can_create_session = AsyncMock(return_value=can_create_session)

    async def require_authenticated(request: Request) -> None:
        if principal is None:
            raise HTTPException(status_code=401, detail="authentication required")
        request.state.uterm_principal = principal

    app.include_router(create_api_router(), dependencies=[Depends(require_authenticated)])
    return app


def _principal(*roles: str, subject_id: str = "caller", scopes: frozenset[str] = frozenset()) -> SimpleNamespace:
    return SimpleNamespace(subject_id=subject_id, roles=frozenset(roles), scopes=scopes)


def _registry() -> MagicMock:
    registry = MagicMock()
    registry.create_session = AsyncMock()
    registry.delete_session = AsyncMock()
    registry.get_definition = AsyncMock(return_value=None)
    return registry


def test_pam_events_rejects_unauthenticated_callers() -> None:
    response = TestClient(_app_for(None, _registry())).post("/api/pam-events", json={})

    assert response.status_code == 401


def test_pam_events_rejects_viewers_before_ingestion() -> None:
    registry = _registry()

    response = TestClient(_app_for(_principal("viewer"), registry)).post(
        "/api/pam-events", json={"event": "open", "username": "alice", "tty": "/dev/pts/3"}
    )

    assert response.status_code == 403
    registry.create_session.assert_not_awaited()


def test_pam_events_rejects_scoped_operator_without_session_create_capability() -> None:
    registry = _registry()
    principal = _principal("operator", scopes=frozenset({"session.read"}))

    response = TestClient(_app_for(principal, registry, can_create_session=False)).post(
        "/api/pam-events", json={"event": "open", "username": "alice", "tty": "/dev/pts/3"}
    )

    assert response.status_code == 403
    registry.create_session.assert_not_awaited()


def test_pam_events_rejects_denied_operator_before_parsing_malformed_json() -> None:
    response = TestClient(_app_for(_principal("operator"), _registry(), can_create_session=False)).post(
        "/api/pam-events", content=b"{"
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "insufficient privileges"


def test_pam_events_allows_scoped_machine_credential_with_session_create_capability() -> None:
    registry = _registry()
    principal = _principal("operator", subject_id="machine:pam-bridge", scopes=frozenset({"session.control.create"}))

    app = _app_for(principal, registry, can_create_session=True)
    response = TestClient(app).post("/api/pam-events", json={"event": "open", "username": "alice", "tty": "/dev/pts/3"})

    assert response.status_code == 200
    app.state.uterm_authz.can_create_session.assert_awaited_once_with(principal)


def test_pam_events_returns_cloudflare_shaped_validation_errors() -> None:
    client = TestClient(_app_for(_principal("operator"), _registry()))

    invalid_json = client.post("/api/pam-events", content=b"{")
    unknown_event = client.post("/api/pam-events", json={"event": "reboot", "username": "alice"})
    missing_username = client.post("/api/pam-events", json={"event": "open", "username": ""})
    invalid_username = client.post("/api/pam-events", json={"event": "open", "username": None})

    assert (invalid_json.status_code, invalid_json.json()) == (400, {"error": "invalid_json"})
    assert (unknown_event.status_code, unknown_event.json()) == (
        422,
        {"error": "unknown_event", "event": "reboot"},
    )
    assert (missing_username.status_code, missing_username.json()) == (422, {"error": "missing_username"})
    assert (invalid_username.status_code, invalid_username.json()) == (422, {"error": "missing_username"})


def test_pam_events_operator_open_matches_cloudflare_contract_without_relay() -> None:
    registry = _registry()

    response = TestClient(_app_for(_principal("operator"), registry)).post(
        "/api/pam-events",
        json={"event": "open", "username": "alice", "tty": "/dev/pts/3", "pid": 123, "mode": "notify"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "session_id": "pam-alice-3", "action": "created"}
    registry.create_session.assert_awaited_once_with(
        {
            "session_id": "pam-alice-3",
            "display_name": "alice (/dev/pts/3)",
            "connector_type": "shell",
            "connector_config": {},
            "input_mode": "open",
            "auto_start": False,
            "ephemeral": True,
            "tags": ["pam", "notify", "alice"],
            "recording_enabled": False,
            "owner": "alice",
            "visibility": "operator",
        }
    )


def test_pam_events_admin_close_matches_cloudflare_contract() -> None:
    registry = _registry()

    response = TestClient(_app_for(_principal("admin"), registry)).post(
        "/api/pam-events", json={"event": "close", "username": "alice", "tty": "/dev/pts/3", "pid": 123}
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "session_id": "pam-alice-3", "action": "deleted"}
    registry.delete_session.assert_awaited_once_with("pam-alice-3")


def test_pam_events_duplicate_open_remains_idempotent() -> None:
    registry = _registry()
    registry.create_session.side_effect = ValueError("session already exists")
    registry.get_definition.return_value = object()

    response = TestClient(_app_for(_principal("operator"), registry)).post(
        "/api/pam-events", json={"event": "open", "username": "alice", "tty": "/dev/pts/3"}
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "session_id": "pam-alice-3", "action": "created"}


def test_pam_events_maps_registry_validation_failure_to_422() -> None:
    registry = _registry()
    registry.create_session.side_effect = SessionValidationError("invalid session")

    response = TestClient(_app_for(_principal("operator"), registry)).post(
        "/api/pam-events", json={"event": "open", "username": "alice", "tty": "/dev/pts/3"}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid session"


def test_pam_events_maps_registry_conflict_to_409() -> None:
    registry = _registry()
    registry.create_session.side_effect = ValueError("session limit reached: max_sessions=1")

    response = TestClient(_app_for(_principal("operator"), registry)).post(
        "/api/pam-events", json={"event": "open", "username": "alice", "tty": "/dev/pts/3"}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "session limit reached: max_sessions=1"
