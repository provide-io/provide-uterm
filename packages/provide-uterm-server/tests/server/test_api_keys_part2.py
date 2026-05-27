#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for API key management — store, auth integration, and HTTP routes."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.api_keys import ApiKeyStore

# ---------------------------------------------------------------------------
# Unit tests: ApiKeyStore
# ---------------------------------------------------------------------------


class TestApiKeyRoutes:
    """Integration tests for the /api/keys endpoints."""

    @pytest.fixture()
    def admin_client(self) -> TestClient:
        config = default_server_config()
        config.auth.mode = "header"
        config.auth.header_mode_acknowledged = True
        config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
        config.auth.api_keys_enabled = True
        app = create_server_app(config)
        return TestClient(app)

    @pytest.fixture()
    def disabled_client(self) -> TestClient:
        config = default_server_config()
        config.auth.mode = "header"
        config.auth.header_mode_acknowledged = True
        config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
        config.auth.api_keys_enabled = False
        app = create_server_app(config)
        return TestClient(app)

    # POST /api/keys

    def test_create_key(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": "my-key", "scopes": ["viewer"]})
        assert resp.status_code == 200
        data = resp.json()
        assert "key" in data
        assert "key_id" in data
        assert data["name"] == "my-key"
        assert data["scopes"] == ["viewer"]

    def test_create_key_with_scopes(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": "scoped", "scopes": ["operator", "viewer"]})
        assert resp.status_code == 200
        assert set(resp.json()["scopes"]) == {"operator", "viewer"}

    def test_create_key_with_expiry(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": "temp", "scopes": ["admin"], "expires_in_s": 3600})
        assert resp.status_code == 200
        assert resp.json()["expires_at"] is not None

    def test_create_key_expiry_too_short(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": "bad", "scopes": ["viewer"], "expires_in_s": 10})
        assert resp.status_code == 422

    def test_create_key_no_name(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": "", "scopes": ["viewer"]})
        assert resp.status_code == 422

    def test_create_key_missing_name(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"scopes": ["viewer"]})
        assert resp.status_code == 422

    def test_create_key_missing_scopes(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": "missing-scopes"})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "scopes is required"

    def test_create_key_scopes_non_list_rejected(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": "bad-scopes", "scopes": "not-a-list"})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "scopes must be a list of role scopes"

    def test_create_key_scopes_empty_rejected(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": "empty-scopes", "scopes": []})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "scopes must include at least one role scope"

    def test_create_key_scopes_invalid_rejected(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": "invalid-scopes", "scopes": ["viewer", "read"]})
        assert resp.status_code == 422
        assert "invalid role scopes: read" in resp.json()["detail"]

    def test_create_key_disabled(self, disabled_client: TestClient) -> None:
        resp = disabled_client.post("/api/keys", json={"name": "nope", "scopes": ["viewer"]})
        assert resp.status_code == 403

    # GET /api/keys

    def test_list_keys_empty(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/keys")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_keys_after_create(self, admin_client: TestClient) -> None:
        admin_client.post("/api/keys", json={"name": "first", "scopes": ["viewer"]})
        admin_client.post("/api/keys", json={"name": "second", "scopes": ["operator"]})
        resp = admin_client.get("/api/keys")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Must NOT expose raw key
        for item in data:
            assert "key" not in item
            assert "key_hash" not in item

    def test_list_keys_disabled(self, disabled_client: TestClient) -> None:
        resp = disabled_client.get("/api/keys")
        assert resp.status_code == 403

    # DELETE /api/keys/{key_id}

    def test_revoke_key(self, admin_client: TestClient) -> None:
        create_resp = admin_client.post("/api/keys", json={"name": "to-revoke", "scopes": ["viewer"]})
        key_id = create_resp.json()["key_id"]
        resp = admin_client.delete(f"/api/keys/{key_id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # Verify it shows as revoked in the list
        keys = admin_client.get("/api/keys").json()
        revoked = [k for k in keys if k["key_id"] == key_id]
        assert len(revoked) == 1
        assert revoked[0]["revoked"] is True

    def test_revoke_unknown_key(self, admin_client: TestClient) -> None:
        resp = admin_client.delete("/api/keys/nonexistent1234")
        assert resp.status_code == 404

    def test_revoke_key_disabled(self, disabled_client: TestClient) -> None:
        resp = disabled_client.delete("/api/keys/whatever")
        assert resp.status_code == 403

    # Auth: revoked key cannot authenticate

    def test_revoked_key_rejected(self, admin_client: TestClient) -> None:
        # Enable api_keys on this client's app
        create_resp = admin_client.post("/api/keys", json={"name": "revokable", "scopes": ["viewer"]})
        raw_key = create_resp.json()["key"]
        key_id = create_resp.json()["key_id"]
        # Verify key works
        store = admin_client.app.state.uterm_api_key_store
        assert store.validate(raw_key) is not None
        # Revoke
        admin_client.delete(f"/api/keys/{key_id}")
        # Verify key no longer validates
        assert store.validate(raw_key) is None

    # Auth: viewer role cannot manage keys

    def test_viewer_cannot_create_keys(self) -> None:
        import jwt as _jwt

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
        config.auth.mode = "jwt"
        config.auth.jwt_public_key_pem = key
        config.auth.worker_bearer_token = "worker-secret"
        config.auth.api_keys_enabled = True
        app = create_server_app(config)
        client = TestClient(app)
        resp = client.post(
            "/api/keys",
            json={"name": "nope", "scopes": ["viewer"]},
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert resp.status_code == 403

    # Scoped keys: principal has correct scopes

    def test_scoped_key_with_role_marker_has_unrestricted_scope(self) -> None:
        """Finding #3: a recognised role scope grants role + ``*`` scope.

        The prior test asserted that a key with capability-style scopes
        (``session.read``, ``session.write``) would authenticate.  That path
        was the same one that silently granted admin to keys with any
        unrecognised scope.  Now: only ``admin``/``operator``/``viewer``
        scopes authenticate, and they grant ``*`` scope (unrestricted).
        """
        from provide.uterm.server.auth import _principal_from_api_key
        from provide.uterm.server.models import AuthConfig

        store = ApiKeyStore()
        raw_key, _record = store.create("scoped", scopes=frozenset({"operator"}))
        auth = AuthConfig(api_keys_enabled=True, mode="dev_token")
        principal = _principal_from_api_key({"x-api-key": raw_key}, auth, store)
        assert principal is not None
        assert principal.roles == frozenset({"operator"})
        assert principal.scopes == frozenset({"*"})
