#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Security and regression tests for hosted server hardening fixes."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from provide.terminal.client import connect_test_ws
from provide.terminal.server import config_from_mapping, create_server_app, default_server_config
from provide.terminal.server.auth import Principal
from provide.terminal.server.models import AuthConfig, SessionDefinition
from provide.terminal.server.policy import SessionPolicyResolver
from provide.terminal.server.routes.api import create_api_router
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


async def test_policy_uses_trusted_roles_only() -> None:
    policy = SessionPolicyResolver(_jwt_config())
    session = SessionDefinition(session_id="s1", display_name="Session", connector_type="shell")

    role = await policy.role_for(Principal(subject_id="user-1", roles=frozenset({"viewer"})), session)

    assert role == "viewer"


def test_jwt_mode_requires_auth_for_api_and_ws_routes() -> None:
    config = default_server_config()
    config.auth = _jwt_config()
    app = create_server_app(config)

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 401

        with pytest.raises(WebSocketDisconnect), connect_test_ws(client, "/ws/browser/provide-shell/term"):
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
        connect_test_ws(
            client,
            "/ws/worker/provide-shell/term",
            headers={"Authorization": f"Bearer {config.auth.worker_bearer_token}"},
        ) as worker,
    ):
        msg = worker.receive_json()
        assert msg["type"] == "snapshot_req"

        with connect_test_ws(
            client,
            "/ws/browser/provide-shell/term",
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
            json={"session_id": "v1", "display_name": "viewer-created", "connector_type": "shell"},
        )
        assert create_forbidden.status_code == 403

        # operator can create but owner is forced to self when not admin
        created = client.post(
            "/api/sessions",
            headers=_jwt_headers(sub="op-1", roles=["operator"]),
            json={
                "session_id": "owned-op",
                "display_name": "Owned",
                "connector_type": "shell",
                "owner": "someone-else",
            },
        )
        assert created.status_code == 403

        created_ok = client.post(
            "/api/sessions",
            headers=_jwt_headers(sub="op-1", roles=["operator"]),
            json={"session_id": "owned-op", "display_name": "Owned", "connector_type": "shell"},
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
        replay = client.get("/ops/replay/provide-shell")

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


@pytest.mark.asyncio
async def test_cancel_and_wait_cancels_and_drains_pending_tasks() -> None:
    task = asyncio.create_task(asyncio.sleep(60.0))

    await _cancel_and_wait({task})

    assert task.done()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_cancel_and_wait_empty_set_is_noop() -> None:
    """Line 42->exit: calling _cancel_and_wait with an empty set does nothing."""
    await _cancel_and_wait(set())


# ---------------------------------------------------------------------------
# Third-review regression tests
# ---------------------------------------------------------------------------


def test_session_id_path_pattern_rejects_invalid_characters() -> None:
    r"""FastAPI path pattern ^[\w\-]+$ should reject dots, slashes, and other chars."""
    config = default_server_config()
    app = create_server_app(config)
    with TestClient(app) as client:
        r = client.get("/api/sessions/bad.session.id")
        assert r.status_code == 422

        r = client.get("/api/sessions/contains space")
        assert r.status_code == 422


def test_events_limit_query_rejects_out_of_range() -> None:
    """Query(ge=1, le=500) on limit means 0 and 501 must return 422."""
    config = default_server_config()
    app = create_server_app(config)
    with TestClient(app) as client:
        r = client.get("/api/sessions/provide-shell/events?limit=0")
        assert r.status_code == 422

        r = client.get("/api/sessions/provide-shell/events?limit=501")
        assert r.status_code == 422

        r = client.get("/api/sessions/provide-shell/recording/entries?limit=0")
        assert r.status_code == 422


def test_health_returns_503_when_registry_not_initialized() -> None:
    """GET /api/health must return 503 when the registry is absent from app state."""
    bare = FastAPI()
    bare.include_router(create_api_router())
    with TestClient(bare) as client:
        r = client.get("/api/health")
        assert r.status_code == 503
        data = r.json()
        assert data["ok"] is False
        assert data["ready"] is False


def test_patch_session_rejects_invalid_input_mode() -> None:
    """PATCH /api/sessions/{id} with an unrecognised input_mode must return 422."""
    config = default_server_config()
    app = create_server_app(config)
    with TestClient(app) as client:
        r = client.patch("/api/sessions/provide-shell", json={"input_mode": "garbage"})
        assert r.status_code == 422


def test_delete_session_idempotent_returns_404_on_second_call() -> None:
    """A second DELETE for a removed session returns 404, not 500."""
    config = default_server_config()
    app = create_server_app(config)
    with TestClient(app) as client:
        r1 = client.delete("/api/sessions/provide-shell")
        assert r1.status_code == 200

        r2 = client.delete("/api/sessions/provide-shell")
        assert r2.status_code == 404


def test_create_server_app_rejects_none_jwt_algorithm() -> None:
    """'none' in jwt_algorithms must be rejected at app construction time."""
    config = default_server_config()
    config.auth.mode = "jwt"
    config.auth.jwt_algorithms = ["none"]
    config.auth.jwt_public_key_pem = "uterm-test-secret-32-byte-minimum-key"
    config.auth.worker_bearer_token = "dummy-token"
    with pytest.raises(ValueError, match="'none' is not permitted"):
        create_server_app(config)


def test_page_route_returns_403_for_private_session_viewer() -> None:
    """A viewer-role principal must receive 403 on a private-visibility session page."""
    config = default_server_config()
    config.sessions[0].visibility = "private"
    app = create_server_app(config)
    with TestClient(app) as client:
        r = client.get(
            f"{config.ui.app_path}/session/provide-shell",
            headers={"x-uterm-role": "viewer"},
        )
        assert r.status_code == 403


def test_config_from_mapping_rejects_negative_max_bytes() -> None:
    """recording.max_bytes < 0 must raise ValueError at config load time."""
    with pytest.raises(ValueError, match="max_bytes"):
        config_from_mapping({"recording": {"max_bytes": -1}})


def test_jwt_without_optional_claims_authenticates_successfully() -> None:
    """Tokens without iat/nbf must be accepted (only sub+exp are required)."""
    config = default_server_config()
    config.auth = _jwt_config()
    app = create_server_app(config)

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "alice",
            "roles": ["admin"],
            "iss": "provide-terminal",
            "aud": "provide-terminal-server",
            "exp": now + 600,
            # No "iat", no "nbf"
        },
        key=_TEST_SIGNING_KEY,
        algorithm="HS256",
    )

    with TestClient(app) as client:
        r = client.get("/api/sessions", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200


def test_recording_download_rejects_path_outside_recording_dir(tmp_path: Path) -> None:
    """Recording download must return 404 if the resolved path escapes the recordings dir."""
    config = default_server_config()
    config.recording.directory = tmp_path / "recordings"
    config.recording.directory.mkdir()
    app = create_server_app(config)

    # Create a file that exists but lives outside the configured recordings directory.
    outside_file = tmp_path / "evil.jsonl"
    outside_file.write_text("{}\n", encoding="utf-8")

    async def _fake_recording_path(session_id: str) -> Path:
        return outside_file

    with TestClient(app) as client:
        app.state.uterm_registry.recording_path = _fake_recording_path
        r = client.get("/api/sessions/provide-shell/recording/download")
        assert r.status_code == 404


def test_ssh_connector_raises_without_known_hosts() -> None:
    """SshSessionConnector must reject a config with no known_hosts and no explicit opt-out."""
    pytest.importorskip("asyncssh", reason="asyncssh not installed")
    from provide.terminal.server.connectors.ssh import SshSessionConnector

    with pytest.raises(ValueError, match="known_hosts"):
        SshSessionConnector("sess1", "Session 1", {"host": "localhost"})


# ---------------------------------------------------------------------------
# Fourth-review regression tests
# ---------------------------------------------------------------------------


def test_create_session_rejects_unknown_connector_type() -> None:
    """POST /api/sessions with an unsupported connector_type must return 422."""
    config = default_server_config()
    app = create_server_app(config)
    with TestClient(app) as client:
        r = client.post(
            "/api/sessions",
            json={"session_id": "bad-type", "display_name": "Bad", "connector_type": "bogus"},
        )
        assert r.status_code == 422


def test_telnet_client_connect_timeout_parameter() -> None:
    """TelnetClient must accept a connect_timeout parameter."""
    from provide.terminal.transports.telnet import TelnetClient

    client = TelnetClient("127.0.0.1", 9, connect_timeout=5.0)
    assert client._connect_timeout == 5.0


def test_set_mode_invalid_value_returns_422() -> None:
    """POST /api/sessions/{id}/mode with invalid input_mode must return 422, not 400."""
    config = default_server_config()
    app = create_server_app(config)
    with TestClient(app) as client:
        r = client.post("/api/sessions/provide-shell/mode", json={"input_mode": "garbage"})
        assert r.status_code == 422


def test_recording_download_denied_when_config_absent() -> None:
    """Recording download must return 404 when uterm_config is not set on app state."""
    from pathlib import Path

    config = default_server_config()
    config.recording.directory = Path(".uterm-recordings")
    app = create_server_app(config)

    async def _fake_path(session_id: str) -> Path:
        return config.recording.directory / f"{session_id}.jsonl"

    # Remove uterm_config from state so the containment guard has no reference dir.
    with TestClient(app) as client:
        del app.state.uterm_config
        app.state.uterm_registry.recording_path = _fake_path
        r = client.get("/api/sessions/provide-shell/recording/download")
        assert r.status_code == 404


def test_replay_log_speed_clamps_to_maximum(tmp_path: Path) -> None:
    """replay_log speed is clamped to 100.0; absurdly large values don't loop infinitely."""
    import io
    import time

    from provide.terminal.replay.viewer import replay_log

    log = tmp_path / "session.jsonl"
    now = time.time()
    log.write_text(
        f'{{"event": "read", "ts": {now}, "data": {{"screen": "A"}}}}\n'
        f'{{"event": "read", "ts": {now + 60.0}, "data": {{"screen": "B"}}}}\n',
        encoding="utf-8",
    )
    buf = io.StringIO()
    start = time.monotonic()
    replay_log(log, speed=1_000_000.0, output=buf)
    elapsed = time.monotonic() - start
    # At 100× max clamp, a 60 s gap becomes 0.6 s — well under 5 s.
    assert elapsed < 5.0
    assert "B" in buf.getvalue()


def test_session_definition_has_no_last_active_at_field() -> None:
    """SessionDefinition must not expose last_active_at (field removed in fourth review)."""
    from provide.terminal.server.models import SessionDefinition

    sd = SessionDefinition(session_id="s1", display_name="S1", connector_type="shell")
    assert not hasattr(sd, "last_active_at")


def test_ssh_connector_allows_insecure_no_host_check_flag() -> None:
    """insecure_no_host_check=True must bypass the known_hosts requirement with a warning."""
    pytest.importorskip("asyncssh", reason="asyncssh not installed")
    from provide.terminal.server.connectors.ssh import SshSessionConnector

    connector = SshSessionConnector("sess1", "Session 1", {"host": "localhost", "insecure_no_host_check": True})
    assert connector._known_hosts is None


# Fifth- and sixth-review regression tests have been moved to
# test_server_security_regressions_late.py to keep this file under 500 LOC.
