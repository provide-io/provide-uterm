#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for API key management — store, auth integration, and HTTP routes."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.api_keys import ApiKey, ApiKeyStore, _hash_key

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
        raw_key, record = store.create("my-key", tenant_id="tenant-a")
        store.revoke_for_tenant(record.key_id, "tenant-a")
        assert store.validate(raw_key) is None

    def test_validate_expired_key(self) -> None:
        store = ApiKeyStore()
        raw_key, _record = store.create("my-key", expires_in_s=1)
        # Expire the key by shifting time
        with patch("provide.uterm.server.api_keys.time") as mock_time:
            mock_time.time.return_value = time.time() + 3600
            assert store.validate(raw_key) is None

    def test_validate_not_yet_expired_key(self) -> None:
        store = ApiKeyStore()
        raw_key, _record = store.create("my-key", expires_in_s=3600)
        assert store.validate(raw_key) is not None

    def test_revoke_returns_true_for_existing(self) -> None:
        store = ApiKeyStore()
        _raw_key, record = store.create("my-key", tenant_id="tenant-a")
        assert store.revoke_for_tenant(record.key_id, "tenant-a") is True
        assert record.revoked is True

    def test_revoke_returns_false_for_unknown(self) -> None:
        store = ApiKeyStore()
        assert store.revoke_for_tenant("nonexistent", "tenant-a") is False

    def test_list_keys(self) -> None:
        store = ApiKeyStore()
        store.create("key-a", tenant_id="tenant-a")
        store.create("key-b", tenant_id="tenant-a")
        keys = store.list_keys_for_tenant("tenant-a")
        assert len(keys) == 2
        names = {k.name for k in keys}
        assert names == {"key-a", "key-b"}

    def test_tenant_scoped_list_and_atomic_revoke_conceal_foreign_keys(self) -> None:
        store = ApiKeyStore()
        _raw_a, key_a = store.create("key-a", tenant_id="tenant-a")
        _raw_b, key_b = store.create("key-b", tenant_id="tenant-b")

        assert store.list_keys_for_tenant("tenant-a") == [key_a]
        assert store.revoke_for_tenant(key_b.key_id, "tenant-a") is False
        assert store.revoke_for_tenant(key_a.key_id, "tenant-a") is True
        assert store.revoke_for_tenant(key_a.key_id, "tenant-a") is True
        assert key_a.revoked is True
        assert key_b.revoked is False

    def test_concurrent_scoped_revoke_never_authorizes_foreign_tenant(self) -> None:
        store = ApiKeyStore()
        _raw, key = store.create("shared-race", tenant_id="tenant-a")

        with ThreadPoolExecutor(max_workers=8) as pool:
            foreign = list(pool.map(lambda _index: store.revoke_for_tenant(key.key_id, "tenant-b"), range(32)))
            own = list(pool.map(lambda _index: store.revoke_for_tenant(key.key_id, "tenant-a"), range(32)))

        assert foreign == [False] * 32
        assert own == [True] * 32
        assert key.revoked is True

    def test_create_with_scopes(self) -> None:
        store = ApiKeyStore()
        _raw, record = store.create("scoped", scopes=frozenset({"read", "write"}))
        assert record.scopes == frozenset({"read", "write"})

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
            "provide.uterm.server.api_keys.secrets.compare_digest", wraps=__import__("secrets").compare_digest
        ) as mock_cmp:
            store.validate(raw_key)
            assert mock_cmp.called


# ---------------------------------------------------------------------------
# Unit tests: ApiKey dataclass
# ---------------------------------------------------------------------------


class TestApiKeyDataclass:
    def test_legacy_positional_signature_is_compatible(self) -> None:
        key = ApiKey("id", "hash", "name", frozenset({"admin"}), 1.0, 2.0, 3.0, True)

        assert key.scopes == frozenset({"admin"})
        assert key.created_at == 1.0
        assert key.expires_at == 2.0
        assert key.last_used_at == 3.0
        assert key.revoked is True
        assert key.tenant_id is None

    def test_defaults(self) -> None:
        key = ApiKey(key_id="abc", key_hash="def", name="test")
        assert key.revoked is False
        assert key.scopes == frozenset()
        assert key.last_used_at is None
        assert key.expires_at is None


# ---------------------------------------------------------------------------
# Auth integration tests
# ---------------------------------------------------------------------------


class TestApiKeyAuthIntegration:
    """Test that X-API-Key header authenticates requests."""

    @pytest.fixture()
    def api_key_client(self) -> tuple[TestClient, str]:
        """Create app with API keys enabled in dev mode, return (client, raw_key).

        Uses context-manager so lifespan runs and uterm_ready=True before tests.
        """
        config = default_server_config()
        config.auth.mode = "header"
        config.auth.header_mode_acknowledged = True
        config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
        config.auth.api_keys_enabled = True
        app = create_server_app(config)
        store = app.state.uterm_api_key_store
        raw_key, _record = store.create("integration-test", scopes=frozenset({"admin"}))
        with TestClient(app) as client:
            yield client, raw_key

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
        config.auth.mode = "header"
        config.auth.header_mode_acknowledged = True
        config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
        config.auth.api_keys_enabled = True
        app = create_server_app(config)
        client = TestClient(app)
        resp = client.get("/api/sessions")
        assert resp.status_code == 200

    def test_api_keys_disabled_ignores_header(self) -> None:
        config = default_server_config()
        config.auth.mode = "header"
        config.auth.header_mode_acknowledged = True
        config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
        config.auth.api_keys_enabled = False
        app = create_server_app(config)
        store = app.state.uterm_api_key_store
        raw_key, _record = store.create("should-be-ignored", scopes=frozenset({"admin"}))
        client = TestClient(app)
        # Falls through to dev auth
        resp = client.get("/api/sessions", headers={"X-API-Key": raw_key})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth principal details
# ---------------------------------------------------------------------------


class TestApiKeyPrincipalRoles:
    """Test role mapping from API key scopes."""

    def test_empty_scopes_rejected(self) -> None:
        """Finding #3: a key minted with no scope must be rejected (was: admin)."""
        from provide.uterm.server.auth import _principal_from_api_key
        from provide.uterm.server.models import AuthConfig

        store = ApiKeyStore()
        raw_key, _record = store.create("admin-key")  # no scopes
        auth = AuthConfig(api_keys_enabled=True, mode="dev_token")
        principal = _principal_from_api_key({"x-api-key": raw_key}, auth, store)
        # An empty-scope key used to silently authenticate as admin; now it is
        # treated as an unknown key and rejected.
        assert principal is None

    def test_admin_scope_gets_admin(self) -> None:
        from provide.uterm.server.auth import _principal_from_api_key
        from provide.uterm.server.models import AuthConfig

        store = ApiKeyStore()
        raw_key, _record = store.create("admin-key", scopes=frozenset({"admin"}))
        auth = AuthConfig(api_keys_enabled=True, mode="dev_token")
        principal = _principal_from_api_key({"x-api-key": raw_key}, auth, store)
        assert principal is not None
        assert "admin" in principal.roles

    def test_operator_scope_gets_operator(self) -> None:
        from provide.uterm.server.auth import _principal_from_api_key
        from provide.uterm.server.models import AuthConfig

        store = ApiKeyStore()
        raw_key, _record = store.create("op-key", scopes=frozenset({"operator"}))
        auth = AuthConfig(api_keys_enabled=True, mode="dev_token")
        principal = _principal_from_api_key({"x-api-key": raw_key}, auth, store)
        assert principal is not None
        assert "operator" in principal.roles

    def test_unknown_capability_scope_rejected(self) -> None:
        """Finding #3: capability-only / unrecognised scopes are rejected.

        Previously a key minted with ``scopes={"session.read"}`` (or any
        non-role scope, including typos like ``"administrator"``) silently
        authenticated as admin with the scope used as a capability allowlist.
        Now: only ``admin``/``operator``/``viewer`` scopes are accepted; any
        other scope-only key is treated as unknown.
        """
        from provide.uterm.server.auth import _principal_from_api_key
        from provide.uterm.server.models import AuthConfig

        store = ApiKeyStore()
        raw_key, _record = store.create("read-key", scopes=frozenset({"session.read"}))
        auth = AuthConfig(api_keys_enabled=True, mode="dev_token")
        principal = _principal_from_api_key({"x-api-key": raw_key}, auth, store)
        assert principal is None

    def test_typo_scope_rejected(self) -> None:
        """Finding #3: a typo in the scope name no longer grants admin."""
        from provide.uterm.server.auth import _principal_from_api_key
        from provide.uterm.server.models import AuthConfig

        store = ApiKeyStore()
        raw_key, _record = store.create("typo-key", scopes=frozenset({"administrator"}))
        auth = AuthConfig(api_keys_enabled=True, mode="dev_token")
        principal = _principal_from_api_key({"x-api-key": raw_key}, auth, store)
        assert principal is None

    async def test_viewer_role_marker_gets_viewer_role(self) -> None:
        """Scope {viewer} is a role marker — gives viewer role with unrestricted scope."""
        from provide.uterm.server.auth import _principal_from_api_key
        from provide.uterm.server.authorization import AuthorizationService
        from provide.uterm.server.models import AuthConfig

        store = ApiKeyStore()
        raw_key, _record = store.create("viewer-key", scopes=frozenset({"viewer"}))
        auth = AuthConfig(api_keys_enabled=True, mode="dev_token")
        principal = _principal_from_api_key({"x-api-key": raw_key}, auth, store)
        assert principal is not None
        assert principal.roles == frozenset({"viewer"})
        authz = AuthorizationService()
        # Viewer role — read caps only, unrestricted scope
        assert await authz.has_capability(principal, "session.read") is True
        assert await authz.has_capability(principal, "session.control.create") is False

    def test_disabled_returns_none(self) -> None:
        from provide.uterm.server.auth import _principal_from_api_key
        from provide.uterm.server.models import AuthConfig

        auth = AuthConfig(api_keys_enabled=False, mode="dev_token")
        result = _principal_from_api_key({"x-api-key": "some-key"}, auth, None)
        assert result is None

    def test_empty_header_returns_none(self) -> None:
        from provide.uterm.server.auth import _principal_from_api_key
        from provide.uterm.server.models import AuthConfig

        store = ApiKeyStore()
        auth = AuthConfig(api_keys_enabled=True, mode="dev_token")
        result = _principal_from_api_key({"x-api-key": ""}, auth, store)
        assert result is None

    def test_no_store_returns_none(self) -> None:
        from provide.uterm.server.auth import _principal_from_api_key
        from provide.uterm.server.models import AuthConfig

        auth = AuthConfig(api_keys_enabled=True, mode="dev_token")
        result = _principal_from_api_key({"x-api-key": "some-key"}, auth, None)
        assert result is None

    def test_per_app_isolation(self) -> None:
        """Two apps with separate stores must not share key validity."""
        from provide.uterm.server.auth import _principal_from_api_key
        from provide.uterm.server.models import AuthConfig

        store_a = ApiKeyStore()
        store_b = ApiKeyStore()
        # Finding #3: a key must have a recognised role scope to authenticate.
        raw_key_a, _ = store_a.create("app-a-key", scopes=frozenset({"admin"}))
        auth = AuthConfig(api_keys_enabled=True, mode="dev_token")
        # Key from store_a validates under store_a
        assert _principal_from_api_key({"x-api-key": raw_key_a}, auth, store_a) is not None
        # ...but NOT under store_b
        assert _principal_from_api_key({"x-api-key": raw_key_a}, auth, store_b) is None

    def test_invalid_key_returns_none(self) -> None:
        from provide.uterm.server.auth import _principal_from_api_key
        from provide.uterm.server.models import AuthConfig

        store = ApiKeyStore()
        store.create("real-key", scopes=frozenset({"admin"}))
        auth = AuthConfig(api_keys_enabled=True, mode="dev_token")
        result = _principal_from_api_key({"x-api-key": "wrong-key"}, auth, store)
        assert result is None


# ---------------------------------------------------------------------------
# HTTP route tests: /api/keys
# ---------------------------------------------------------------------------
