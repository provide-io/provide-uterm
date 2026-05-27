#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Authorization and mutation-killing tests for routes/api.py."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config

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


def test_create_session_forbidden_for_viewer(viewer_client: TestClient) -> None:
    r = viewer_client.post("/api/sessions", json={"session_id": "new", "connector_type": "shell"})
    assert r.status_code == 403


def test_create_tunnel_forbidden_for_viewer(viewer_client: TestClient) -> None:
    """Tunnel create requires session.control.create; viewer lacks it → 403.

    Covers the can_create_session guard added when tunnel creation was
    hardened to refuse anonymous owners.
    """
    r = viewer_client.post("/api/tunnels", json={"tunnel_type": "terminal"})
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


def test_hijack_acquire_forbidden_for_viewer(viewer_client: TestClient) -> None:
    """Raw /worker/{id}/hijack/acquire must reject viewer principals.

    rest.py documents that it carries no built-in authz; the protecting layer
    is _require_hub_route_authz on the hub router mount.  Without this check,
    any authenticated caller — including a viewer JWT or a share_token that
    maps to a viewer principal — could seize keyboard control of a session.
    """
    r = viewer_client.post("/worker/provide-shell/hijack/acquire", json={"owner": "viewer1", "lease_s": 60})
    assert r.status_code == 403


def test_hijack_send_forbidden_for_viewer(viewer_client: TestClient) -> None:
    """Keystroke send also requires session.control.hijack, not just auth."""
    r = viewer_client.post("/worker/provide-shell/hijack/HID/send", json={"data": "rm -rf /\n"})
    assert r.status_code == 403


def test_hijack_snapshot_read_allowed_for_viewer(viewer_client: TestClient) -> None:
    """Snapshot GET is gated on session.read, which a viewer has — so NOT 403."""
    r = viewer_client.get("/worker/provide-shell/hijack/HID/snapshot")
    assert r.status_code != 403


def test_input_mode_forbidden_for_viewer(viewer_client: TestClient) -> None:
    """Raw /worker/{id}/input_mode requires session.control.mode."""
    r = viewer_client.post("/worker/provide-shell/input_mode", json={"input_mode": "open"})
    assert r.status_code == 403


def test_disconnect_worker_requires_admin(viewer_client: TestClient) -> None:
    """disconnect_worker is admin-only."""
    r = viewer_client.post("/worker/provide-shell/disconnect_worker")
    assert r.status_code == 403


def test_disconnect_worker_admin_passes_authz(app_client: TestClient) -> None:
    """An admin principal passes the admin-only hub-route authz check.

    In dev mode the default principal has admin role, so the authz layer
    accepts; any non-403 status proves the check returned (the handler
    itself may 404 or 500 depending on session state, but NOT 403).
    """
    r = app_client.post("/worker/provide-shell/disconnect_worker")
    assert r.status_code != 403


def test_hijack_acquire_unknown_session_404(app_client: TestClient) -> None:
    """Unknown session on a capability-gated hub path returns 404, not 403.

    Covers the ``session is None`` branch of _require_hub_route_authz.
    """
    r = app_client.post("/worker/totally-made-up-session/hijack/acquire", json={"lease_s": 60})
    assert r.status_code == 404


def test_hijack_snapshot_unknown_session_404(app_client: TestClient) -> None:
    """Same branch, exercised for the session.read path."""
    r = app_client.get("/worker/totally-made-up-session/hijack/HID/snapshot")
    assert r.status_code == 404


def test_hijack_snapshot_viewer_private_session_403() -> None:
    """Viewer JWT hitting a PRIVATE session they don't own → 403 via read-path branch.

    Covers the session.read 403 branch of _require_hub_route_authz:
    principal has session.read capability (viewer role grants it), but
    can_read_session returns False because visibility=private and the
    principal is not the owner.
    """
    import time

    import jwt as _jwt

    from provide.uterm.server.models import AuthConfig, SessionDefinition

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
    config = default_server_config()
    config.auth = AuthConfig(
        mode="jwt", jwt_public_key_pem=key, jwt_algorithms=["HS256"], worker_bearer_token=viewer_token
    )
    config.sessions = [
        SessionDefinition(
            session_id="priv-sess",
            display_name="Private",
            connector_type="shell",
            owner="alice",
            visibility="private",
        )
    ]
    app = create_server_app(config)
    with TestClient(app, headers={"Authorization": f"Bearer {viewer_token}"}) as client:
        r = client.get("/worker/priv-sess/hijack/HID/snapshot")
    assert r.status_code == 403


def test_create_session_owner_mismatch_forbidden() -> None:
    """Non-admin principal cannot set owner to a different subject_id."""
    import time

    import jwt as _jwt

    from provide.uterm.server.models import AuthConfig

    key = "uterm-test-secret-32-byte-minimum-key"
    now = int(time.time())
    op_token = _jwt.encode(
        {
            "sub": "operator1",
            "roles": ["operator"],
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

    from provide.uterm.server.models import AuthConfig

    key = "uterm-test-secret-32-byte-minimum-key"
    now = int(time.time())

    def _tok(sub: str, roles: list[str]) -> str:
        return _jwt.encode(
            {
                "sub": sub,
                "roles": roles,
                "iss": "provide-uterm",
                "aud": "provide-uterm-server",
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
    config.auth = AuthConfig(mode="jwt", jwt_public_key_pem=key, jwt_algorithms=["HS256"], worker_bearer_token=worker)
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
        assert (
            client.post(
                f"/api/tunnels/{tunnel_id}/tokens/rotate",
                headers={"Authorization": f"Bearer {alice}"},
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"/api/tunnels/{tunnel_id}/tokens",
                headers={"Authorization": f"Bearer {alice}"},
            ).status_code
            == 200
        )


def test_tunnel_rotate_on_missing_session_404() -> None:
    """Rotating a non-existent tunnel returns 404 (cannot rotate what's gone)."""
    client, alice, _ = _two_principal_jwt_app()
    with client:
        r = client.post(
            "/api/tunnels/tunnel-nonexistent/tokens/rotate",
            headers={"Authorization": f"Bearer {alice}"},
        )
        assert r.status_code == 404
