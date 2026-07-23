# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI parity tests for the shared PAM event ingestion route."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from provide.uterm.server.routes.api import create_api_router


def _app_for(principal: object | None, registry: MagicMock) -> FastAPI:
    app = FastAPI()
    app.state.uterm_registry = registry
    app.state.uterm_authz = MagicMock()
    app.state.uterm_authz.is_admin = AsyncMock(
        side_effect=lambda value: "admin" in getattr(value, "roles", frozenset())
    )

    async def require_authenticated(request: Request) -> None:
        if principal is None:
            raise HTTPException(status_code=401, detail="authentication required")
        request.state.uterm_principal = principal

    app.include_router(create_api_router(), dependencies=[Depends(require_authenticated)])
    return app


def _principal(*roles: str) -> SimpleNamespace:
    return SimpleNamespace(subject_id="caller", roles=frozenset(roles))


def _registry() -> MagicMock:
    registry = MagicMock()
    registry.create_session = AsyncMock()
    registry.delete_session = AsyncMock()
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
