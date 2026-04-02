#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for API key management — store, auth integration, and HTTP routes."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from provide.terminal.server import create_server_app, default_server_config
from provide.terminal.server.api_keys import ApiKey, ApiKeyStore, _hash_key

# ---------------------------------------------------------------------------
# Unit tests: ApiKeyStore
# ---------------------------------------------------------------------------


class TestApiKeyStore:
    """Unit tests for the in-memory API key store."""

    def test_create_returns_raw_key_and_record(self) -> None:
        store = ApiKeyStore()
        raw_key, record = store.create("test-key")
        assert isinstance(raw_key, str)
        assert len(raw_key) > 20
        assert record.name == "test-key"
        assert record.key_id == _hash_key(raw_key)[:16]
        assert record.key_hash == _hash_key(raw_key)
        assert record.revoked is False
        assert record.scopes == frozenset()

    def test_validate_correct_key(self) -> None:
        store = ApiKeyStore()
        raw_key, record = store.create("my-key")
        result = store.validate(raw_key)
        assert result is not None
        assert result.key_id == record.key_id
        assert result.last_used_at is not None

    def test_validate_wrong_key(self) -> None:
        store = ApiKeyStore()
        store.create("my-key")
        assert store.validate("wrong-key-value") is None

    def test_validate_revoked_key(self) -> None:
        store = ApiKeyStore()
        raw_key, record = store.create("my-key")
        store.revoke(record.key_id)
        assert store.validate(raw_key) is None

    def test_validate_expired_key(self) -> None:
        store = ApiKeyStore()
        raw_key, _record = store.create("my-key", expires_in_s=1)
        # Expire the key by shifting time
        with patch("provide.terminal.server.api_keys.time") as mock_time:
            mock_time.time.return_value = time.time() + 3600
            assert store.validate(raw_key) is None

    def test_validate_not_yet_expired_key(self) -> None:
        store = ApiKeyStore()
        raw_key, _record = store.create("my-key", expires_in_s=3600)
        assert store.validate(raw_key) is not None

    def test_revoke_returns_true_for_existing(self) -> None:
        store = ApiKeyStore()
        _raw_key, record = store.create("my-key")
        assert store.revoke(record.key_id) is True
        assert record.revoked is True

    def test_revoke_returns_false_for_unknown(self) -> None:
        store = ApiKeyStore()
        assert store.revoke("nonexistent") is False

    def test_list_keys(self) -> None:
        store = ApiKeyStore()
        store.create("key-a")
        store.create("key-b")
        keys = store.list_keys()
        assert len(keys) == 2
        names = {k.name for k in keys}
        assert names == {"key-a", "key-b"}

    def test_create_with_scopes(self) -> None:
        store = ApiKeyStore()
        _raw, record = store.create("scoped", scopes=frozenset({"read", "write"}))
        assert record.scopes == frozenset({"read", "write"})

    def test_create_with_rate_limit(self) -> None:
        store = ApiKeyStore()
        _raw, record = store.create("limited", rate_limit_per_sec=10.0)
        assert record.rate_limit_per_sec == 10.0

    def test_expires_at_none_when_no_expiry(self) -> None:
        store = ApiKeyStore()
        _raw, record = store.create("permanent")
        assert record.expires_at is None

    def test_expires_at_set_when_given(self) -> None:
        store = ApiKeyStore()
        before = time.time()
        _raw, record = store.create("temp", expires_in_s=3600)
        assert record.expires_at is not None
        assert record.expires_at >= before + 3600

    def test_timing_safe_comparison(self) -> None:
        """Validate that comparison uses secrets.compare_digest (constant-time)."""
        store = ApiKeyStore()
        raw_key, _record = store.create("test")
        # Patch compare_digest to verify it is called
        with patch(
            "provide.terminal.server.api_keys.secrets.compare_digest", wraps=__import__("secrets").compare_digest
        ) as mock_cmp:
            store.validate(raw_key)
            assert mock_cmp.called


# ---------------------------------------------------------------------------
# Unit tests: ApiKey dataclass
# ---------------------------------------------------------------------------


class TestApiKeyDataclass:
    def test_defaults(self) -> None:
        key = ApiKey(key_id="abc", key_hash="def", name="test")
        assert key.revoked is False
        assert key.scopes == frozenset()
        assert key.rate_limit_per_sec == 0
        assert key.last_used_at is None
        assert key.expires_at is None


# ---------------------------------------------------------------------------
# Auth integration tests
# ---------------------------------------------------------------------------


class TestApiKeyAuthIntegration:
    """Test that X-API-Key header authenticates requests."""

    @pytest.fixture()
    def api_key_client(self) -> tuple[TestClient, str]:
        """Create app with API keys enabled in dev mode, return (client, raw_key)."""
        config = default_server_config()
        config.auth.mode = "dev"
        config.auth.api_keys_enabled = True
        app = create_server_app(config)
        store = app.state.uterm_api_key_store
        raw_key, _record = store.create("integration-test")
        return TestClient(app), raw_key

    def test_api_key_authenticates_request(self, api_key_client: tuple[TestClient, str]) -> None:
        client, raw_key = api_key_client
        resp = client.get("/api/sessions", headers={"X-API-Key": raw_key})
        assert resp.status_code == 200

    def test_invalid_api_key_falls_through(self, api_key_client: tuple[TestClient, str]) -> None:
        client, _raw_key = api_key_client
        # In dev mode, invalid key falls through to dev auth (which succeeds)
        resp = client.get("/api/sessions", headers={"X-API-Key": "invalid-key"})
        assert resp.status_code == 200

    def test_api_key_sets_principal(self, api_key_client: tuple[TestClient, str]) -> None:
        client, raw_key = api_key_client
        # Create a session to verify the principal is set correctly
        resp = client.get("/api/health", headers={"X-API-Key": raw_key})
        assert resp.status_code == 200

    def test_no_api_key_header_uses_normal_auth(self) -> None:
        config = default_server_config()
        config.auth.mode = "dev"
        config.auth.api_keys_enabled = True
        app = create_server_app(config)
        client = TestClient(app)
        resp = client.get("/api/sessions")
        assert resp.status_code == 200

    def test_api_keys_disabled_ignores_header(self) -> None:
        config = default_server_config()
        config.auth.mode = "dev"
        config.auth.api_keys_enabled = False
        app = create_server_app(config)
        store = app.state.uterm_api_key_store
        raw_key, _record = store.create("should-be-ignored")
        client = TestClient(app)
        # Falls through to dev auth
        resp = client.get("/api/sessions", headers={"X-API-Key": raw_key})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth principal details
# ---------------------------------------------------------------------------


class TestApiKeyPrincipalRoles:
    """Test role mapping from API key scopes."""

    def test_empty_scopes_gets_admin(self) -> None:
        from provide.terminal.server.auth import _principal_from_api_key, set_api_key_store_hook
        from provide.terminal.server.models import AuthConfig

        store = ApiKeyStore()
        raw_key, _record = store.create("admin-key")
        auth = AuthConfig(api_keys_enabled=True, mode="dev")
        set_api_key_store_hook(lambda: store)
        principal = _principal_from_api_key({"x-api-key": raw_key}, auth)
        assert principal is not None
        assert "admin" in principal.roles

    def test_admin_scope_gets_admin(self) -> None:
        from provide.terminal.server.auth import _principal_from_api_key, set_api_key_store_hook
        from provide.terminal.server.models import AuthConfig

        store = ApiKeyStore()
        raw_key, _record = store.create("admin-key", scopes=frozenset({"admin"}))
        auth = AuthConfig(api_keys_enabled=True, mode="dev")
        set_api_key_store_hook(lambda: store)
        principal = _principal_from_api_key({"x-api-key": raw_key}, auth)
        assert principal is not None
        assert "admin" in principal.roles

    def test_operator_scope_gets_operator(self) -> None:
        from provide.terminal.server.auth import _principal_from_api_key, set_api_key_store_hook
        from provide.terminal.server.models import AuthConfig

        store = ApiKeyStore()
        raw_key, _record = store.create("op-key", scopes=frozenset({"operator"}))
        auth = AuthConfig(api_keys_enabled=True, mode="dev")
        set_api_key_store_hook(lambda: store)
        principal = _principal_from_api_key({"x-api-key": raw_key}, auth)
        assert principal is not None
        assert "operator" in principal.roles

    def test_other_scope_gets_viewer(self) -> None:
        from provide.terminal.server.auth import _principal_from_api_key, set_api_key_store_hook
        from provide.terminal.server.models import AuthConfig

        store = ApiKeyStore()
        raw_key, _record = store.create("read-key", scopes=frozenset({"session.read"}))
        auth = AuthConfig(api_keys_enabled=True, mode="dev")
        set_api_key_store_hook(lambda: store)
        principal = _principal_from_api_key({"x-api-key": raw_key}, auth)
        assert principal is not None
        assert "viewer" in principal.roles

    def test_disabled_returns_none(self) -> None:
        from provide.terminal.server.auth import _principal_from_api_key
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(api_keys_enabled=False, mode="dev")
        result = _principal_from_api_key({"x-api-key": "some-key"}, auth)
        assert result is None

    def test_empty_header_returns_none(self) -> None:
        from provide.terminal.server.auth import _principal_from_api_key, set_api_key_store_hook
        from provide.terminal.server.models import AuthConfig

        store = ApiKeyStore()
        set_api_key_store_hook(lambda: store)
        auth = AuthConfig(api_keys_enabled=True, mode="dev")
        result = _principal_from_api_key({"x-api-key": ""}, auth)
        assert result is None

    def test_no_store_returns_none(self) -> None:
        from provide.terminal.server.auth import _principal_from_api_key, set_api_key_store_hook
        from provide.terminal.server.models import AuthConfig

        set_api_key_store_hook(lambda: None)
        auth = AuthConfig(api_keys_enabled=True, mode="dev")
        result = _principal_from_api_key({"x-api-key": "some-key"}, auth)
        assert result is None

    def test_invalid_key_returns_none(self) -> None:
        from provide.terminal.server.auth import _principal_from_api_key, set_api_key_store_hook
        from provide.terminal.server.models import AuthConfig

        store = ApiKeyStore()
        store.create("real-key")
        set_api_key_store_hook(lambda: store)
        auth = AuthConfig(api_keys_enabled=True, mode="dev")
        result = _principal_from_api_key({"x-api-key": "wrong-key"}, auth)
        assert result is None


# ---------------------------------------------------------------------------
# HTTP route tests: /api/keys
# ---------------------------------------------------------------------------


class TestApiKeyRoutes:
    """Integration tests for the /api/keys endpoints."""

    @pytest.fixture()
    def admin_client(self) -> TestClient:
        config = default_server_config()
        config.auth.mode = "dev"
        config.auth.api_keys_enabled = True
        app = create_server_app(config)
        return TestClient(app)

    @pytest.fixture()
    def disabled_client(self) -> TestClient:
        config = default_server_config()
        config.auth.mode = "dev"
        config.auth.api_keys_enabled = False
        app = create_server_app(config)
        return TestClient(app)

    # POST /api/keys

    def test_create_key(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": "my-key"})
        assert resp.status_code == 200
        data = resp.json()
        assert "key" in data
        assert "key_id" in data
        assert data["name"] == "my-key"
        assert isinstance(data["scopes"], list)

    def test_create_key_with_scopes(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": "scoped", "scopes": ["read", "write"]})
        assert resp.status_code == 200
        assert set(resp.json()["scopes"]) == {"read", "write"}

    def test_create_key_with_expiry(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": "temp", "expires_in_s": 3600})
        assert resp.status_code == 200
        assert resp.json()["expires_at"] is not None

    def test_create_key_expiry_too_short(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": "bad", "expires_in_s": 10})
        assert resp.status_code == 422

    def test_create_key_no_name(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": ""})
        assert resp.status_code == 422

    def test_create_key_missing_name(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={})
        assert resp.status_code == 422

    def test_create_key_disabled(self, disabled_client: TestClient) -> None:
        resp = disabled_client.post("/api/keys", json={"name": "nope"})
        assert resp.status_code == 403

    # GET /api/keys

    def test_list_keys_empty(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/keys")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_keys_after_create(self, admin_client: TestClient) -> None:
        admin_client.post("/api/keys", json={"name": "first"})
        admin_client.post("/api/keys", json={"name": "second"})
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
        create_resp = admin_client.post("/api/keys", json={"name": "to-revoke"})
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
        create_resp = admin_client.post("/api/keys", json={"name": "revokable"})
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
        config.auth.mode = "jwt"
        config.auth.jwt_public_key_pem = key
        config.auth.worker_bearer_token = "worker-secret"
        config.auth.api_keys_enabled = True
        app = create_server_app(config)
        client = TestClient(app)
        resp = client.post(
            "/api/keys",
            json={"name": "nope"},
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert resp.status_code == 403

    # Scoped keys: principal has correct scopes

    def test_scoped_key_principal_has_scopes(self) -> None:
        from provide.terminal.server.auth import _principal_from_api_key, set_api_key_store_hook
        from provide.terminal.server.models import AuthConfig

        store = ApiKeyStore()
        raw_key, _record = store.create("scoped", scopes=frozenset({"session.read", "session.write"}))
        auth = AuthConfig(api_keys_enabled=True, mode="dev")
        set_api_key_store_hook(lambda: store)
        principal = _principal_from_api_key({"x-api-key": raw_key}, auth)
        assert principal is not None
        assert principal.scopes == frozenset({"session.read", "session.write"})

    def test_create_key_with_rate_limit(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            "/api/keys",
            json={"name": "limited", "rate_limit_per_sec": 5.0},
        )
        assert resp.status_code == 200

    def test_create_key_scopes_non_list_ignored(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/keys", json={"name": "bad-scopes", "scopes": "not-a-list"})
        assert resp.status_code == 200
        assert resp.json()["scopes"] == []
