#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for structured audit logging."""

from __future__ import annotations

import time
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient

from provide.terminal.server import create_server_app, default_server_config
from provide.terminal.server.audit import audit_event

_TEST_KEY = "uterm-test-secret-32-byte-minimum-key"


# ---------------------------------------------------------------------------
# Unit tests for audit_event
# ---------------------------------------------------------------------------


def test_audit_event_emits_structured_log() -> None:
    with patch("provide.terminal.server.audit._audit_log") as mock_log:
        audit_event(
            "session.create",
            principal="user1",
            session_id="s-123",
            source_ip="10.0.0.1",
            detail={"connector_type": "ssh"},
        )
    mock_log.info.assert_called_once()
    args, kwargs = mock_log.info.call_args
    assert "audit action=%s" in args[0]
    assert args[1] == "session.create"
    assert args[2] == "user1"
    assert args[3] == "s-123"
    assert args[4] == "10.0.0.1"
    extra = kwargs["extra"]
    assert extra["audit"] is True
    assert extra["action"] == "session.create"
    assert extra["principal"] == "user1"
    assert extra["session_id"] == "s-123"
    assert extra["source_ip"] == "10.0.0.1"
    assert extra["detail"] == {"connector_type": "ssh"}
    assert isinstance(extra["ts"], float)


def test_audit_event_default_empty_fields() -> None:
    with patch("provide.terminal.server.audit._audit_log") as mock_log:
        audit_event("auth.failure")
    extra = mock_log.info.call_args[1]["extra"]
    assert extra["principal"] == ""
    assert extra["session_id"] == ""
    assert extra["source_ip"] == ""
    assert extra["detail"] == {}


def test_audit_event_with_detail() -> None:
    detail = {"error": "token expired", "code": 401}
    with patch("provide.terminal.server.audit._audit_log") as mock_log:
        audit_event("auth.failure", detail=detail)
    extra = mock_log.info.call_args[1]["extra"]
    assert extra["detail"] == detail


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------


def _make_token(sub: str = "user1", roles: list[str] | None = None) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": sub,
            "roles": roles or ["admin"],
            "iss": "provide-terminal",
            "aud": "provide-terminal-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=_TEST_KEY,
        algorithm="HS256",
    )


@pytest.fixture()
def jwt_client() -> TestClient:
    config = default_server_config()
    config.auth.mode = "jwt"
    config.auth.jwt_public_key_pem = _TEST_KEY
    config.auth.jwt_algorithms = ["HS256"]
    config.auth.worker_bearer_token = "test-worker-token"
    app = create_server_app(config)
    return TestClient(app)


@pytest.fixture()
def dev_client() -> TestClient:
    config = default_server_config()
    config.auth.mode = "dev"
    app = create_server_app(config)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Integration tests — verify audit_event calls during API operations
# ---------------------------------------------------------------------------


def test_session_create_emits_audit(dev_client: TestClient) -> None:
    with patch("provide.terminal.server.routes.api.audit_event") as mock:
        r = dev_client.post("/api/connect", json={"connector_type": "shell"})
        assert r.status_code == 200
        calls = [c for c in mock.call_args_list if c[0][0] == "session.create"]
        assert len(calls) == 1
        kw = calls[0][1]
        assert kw["principal"] == "local-dev"
        assert kw["session_id"].startswith("connect-")


def test_session_delete_emits_audit(dev_client: TestClient) -> None:
    # Create a session first
    r = dev_client.post("/api/connect", json={"connector_type": "shell"})
    session_id = r.json()["session_id"]
    with patch("provide.terminal.server.routes.api.audit_event") as mock:
        r = dev_client.delete(f"/api/sessions/{session_id}")
        assert r.status_code == 200
        calls = [c for c in mock.call_args_list if c[0][0] == "session.delete"]
        assert len(calls) == 1
        assert calls[0][1]["session_id"] == session_id


def test_tunnel_create_emits_audit(dev_client: TestClient) -> None:
    with patch("provide.terminal.server.routes.api.audit_event") as mock:
        r = dev_client.post("/api/tunnels", json={"tunnel_type": "terminal"})
        assert r.status_code == 200
        calls = [c for c in mock.call_args_list if c[0][0] == "tunnel.create"]
        assert len(calls) == 1
        kw = calls[0][1]
        assert kw["session_id"].startswith("tunnel-")
        assert kw["detail"]["tunnel_type"] == "terminal"


def test_token_revoke_emits_audit(dev_client: TestClient) -> None:
    r = dev_client.post("/api/tunnels", json={"tunnel_type": "terminal"})
    tunnel_id = r.json()["tunnel_id"]
    with patch("provide.terminal.server.routes.api.audit_event") as mock:
        r = dev_client.delete(f"/api/tunnels/{tunnel_id}/tokens")
        assert r.status_code == 200
        calls = [c for c in mock.call_args_list if c[0][0] == "tunnel.tokens.revoke"]
        assert len(calls) == 1
        assert calls[0][1]["session_id"] == tunnel_id


def test_token_rotate_emits_audit(dev_client: TestClient) -> None:
    r = dev_client.post("/api/tunnels", json={"tunnel_type": "terminal"})
    tunnel_id = r.json()["tunnel_id"]
    with patch("provide.terminal.server.routes.api.audit_event") as mock:
        r = dev_client.post(f"/api/tunnels/{tunnel_id}/tokens/rotate")
        assert r.status_code == 200
        calls = [c for c in mock.call_args_list if c[0][0] == "tunnel.tokens.rotate"]
        assert len(calls) == 1
        assert calls[0][1]["session_id"] == tunnel_id


def test_auth_success_emits_audit(jwt_client: TestClient) -> None:
    token = _make_token()
    with patch("provide.terminal.server.auth.audit_event") as mock:
        r = jwt_client.get("/api/sessions", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        calls = [c for c in mock.call_args_list if c[0][0] == "auth.success"]
        assert len(calls) >= 1
        assert calls[0][1]["principal"] == "user1"


def test_auth_failure_emits_audit(jwt_client: TestClient) -> None:
    with patch("provide.terminal.server.auth.audit_event") as mock:
        jwt_client.get("/api/sessions", headers={"Authorization": "Bearer invalid-token"})
        calls = [c for c in mock.call_args_list if c[0][0] == "auth.failure"]
        assert len(calls) >= 1
        assert "error" in calls[0][1]["detail"]
