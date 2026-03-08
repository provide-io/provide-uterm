#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Security and regression tests for hosted server hardening fixes."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from provide.terminal.server import create_server_app, default_server_config
from provide.terminal.server.auth import Principal
from provide.terminal.server.models import AuthConfig, SessionDefinition
from provide.terminal.server.policy import SessionPolicyResolver
from provide.terminal.server.runtime import _cancel_and_wait

_TEST_SIGNING_KEY = "uterm-test-secret-32-byte-minimum-key"


def _jwt_headers(
    *, sub: str, roles: list[str], issuer: str = "provide-terminal", audience: str = "provide-terminal-server"
) -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": sub,
            "roles": roles,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=_TEST_SIGNING_KEY,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _jwt_config() -> AuthConfig:
    now = int(time.time())
    worker_token = jwt.encode(
        {
            "sub": "runtime-worker",
            "roles": ["admin"],
            "iss": "provide-terminal",
            "aud": "provide-terminal-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=_TEST_SIGNING_KEY,
        algorithm="HS256",
    )
    return AuthConfig(
        mode="jwt",
        jwt_public_key_pem=_TEST_SIGNING_KEY,
        jwt_algorithms=["HS256"],
        worker_bearer_token=worker_token,
    )


def test_policy_uses_trusted_roles_only() -> None:
    policy = SessionPolicyResolver(_jwt_config())
    session = SessionDefinition(session_id="s1", display_name="Session", connector_type="demo")

    role = policy.role_for(Principal(subject_id="user-1", roles=frozenset({"viewer"})), session)

    assert role == "viewer"


def test_jwt_mode_requires_auth_for_api_and_ws_routes() -> None:
    config = default_server_config()
    config.auth = _jwt_config()
    app = create_server_app(config)

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 401

        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/browser/demo-session/term"):
            pass


def test_jwt_mode_rejects_invalid_issuer() -> None:
    config = default_server_config()
    config.auth = _jwt_config()
    app = create_server_app(config)

    with TestClient(app) as client:
        health = client.get("/api/health", headers=_jwt_headers(sub="alice", roles=["admin"], issuer="wrong-issuer"))
        assert health.status_code == 401


def test_jwt_mode_ignores_cookie_and_role_header_escalation_for_ws() -> None:
    config = default_server_config()
    config.auth = _jwt_config()
    app = create_server_app(config)

    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/ws/worker/demo-session/term", headers=_jwt_headers(sub="worker", roles=["admin"])
        ) as worker,
    ):
        msg = worker.receive_json()
        assert msg["type"] == "snapshot_req"

        with client.websocket_connect(
            "/ws/browser/demo-session/term",
            headers={
                **_jwt_headers(sub="viewer-a", roles=["viewer"]),
                config.auth.role_header: "admin",
                "Cookie": f"{config.auth.surface_cookie}=operator",
            },
        ) as browser:
            hello = browser.receive_json()
            assert hello["type"] == "hello"
            assert hello["role"] == "viewer"
            assert hello["can_hijack"] is False
            assert hello["hijack_control"] == "ws"
            assert hello["hijack_step_supported"] is True


def test_jwt_api_enforces_role_and_ownership() -> None:
    config = default_server_config()
    config.auth = _jwt_config()
    app = create_server_app(config)

    with TestClient(app) as client:
        # viewer may read but cannot mutate
        sessions = client.get("/api/sessions", headers=_jwt_headers(sub="viewer-1", roles=["viewer"]))
        assert sessions.status_code == 200
        assert sessions.json()

        create_forbidden = client.post(
            "/api/sessions",
            headers=_jwt_headers(sub="viewer-1", roles=["viewer"]),
            json={"session_id": "v1", "display_name": "viewer-created", "connector_type": "demo"},
        )
        assert create_forbidden.status_code == 403

        # operator can create but owner is forced to self when not admin
        created = client.post(
            "/api/sessions",
            headers=_jwt_headers(sub="op-1", roles=["operator"]),
            json={"session_id": "owned-op", "display_name": "Owned", "connector_type": "demo", "owner": "someone-else"},
        )
        assert created.status_code == 403

        created_ok = client.post(
            "/api/sessions",
            headers=_jwt_headers(sub="op-1", roles=["operator"]),
            json={"session_id": "owned-op", "display_name": "Owned", "connector_type": "demo"},
        )
        assert created_ok.status_code == 200

        mode_ok = client.post(
            "/api/sessions/owned-op/mode",
            headers=_jwt_headers(sub="op-1", roles=["operator"]),
            json={"input_mode": "hijack"},
        )
        assert mode_ok.status_code == 200

        mode_forbidden = client.post(
            "/api/sessions/owned-op/mode",
            headers=_jwt_headers(sub="op-2", roles=["operator"]),
            json={"input_mode": "open"},
        )
        assert mode_forbidden.status_code == 403

        mode_admin = client.post(
            "/api/sessions/owned-op/mode",
            headers=_jwt_headers(sub="admin-1", roles=["admin"]),
            json={"input_mode": "open"},
        )
        assert mode_admin.status_code == 200


def test_replay_page_honors_custom_app_path() -> None:
    config = default_server_config()
    config.ui.app_path = "/ops"
    app = create_server_app(config)

    with TestClient(app) as client:
        replay = client.get("/ops/replay/demo-session")

    assert replay.status_code == 200
    assert '"app_path": "/ops"' in replay.text


def test_page_routes_set_explicit_cookie_security_flags() -> None:
    config = default_server_config()
    app = create_server_app(config)

    with TestClient(app) as client:
        response = client.get("/app/", headers={"X-Forwarded-Proto": "https"})

    assert response.status_code == 200
    set_cookie = ",".join(response.headers.get_list("set-cookie")).lower()
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "secure" in set_cookie


def test_compiled_frontend_views_escape_dynamic_values() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "provide" / "terminal" / "frontend" / "app" / "views"
    dashboard_js = (root / "dashboard-view.js").read_text(encoding="utf-8")
    operator_js = (root / "operator-view.js").read_text(encoding="utf-8")

    assert "function escapeHtml(value)" in dashboard_js
    assert "function escapeHtml(value)" in operator_js
    assert "const safeAppPath = escapeHtml(appPath);" in dashboard_js
    assert "const safeAppPath = escapeHtml(bootstrap.app_path);" in operator_js
    assert "${bootstrap.app_path}/replay/" not in operator_js


@pytest.mark.asyncio
async def test_cancel_and_wait_cancels_and_drains_pending_tasks() -> None:
    task = asyncio.create_task(asyncio.sleep(60.0))

    await _cancel_and_wait({task})

    assert task.done()
    assert task.cancelled()
