#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Multi-tenancy foundation: tenant validation, tenant-scoped keys, tenant
resolution across header/jwt/api-key modes, the graphical.* capabilities, and
the dev-IdP tenant claim. Mirrors the C#/Go serverauth tenancy tests."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import jwt
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from provide.uterm.server.api_keys import ApiKey, ApiKeyStore, canonical_tenant_id
from provide.uterm.server.auth import (
    _principal_from_api_key,
    _principal_from_header_auth,
    _principal_from_jwt_token,
)
from provide.uterm.server.authorization import ROLE_CAPABILITIES, AuthorizationService
from provide.uterm.server.bridge.identity import Principal
from provide.uterm.server.dev_idp import setup_dev_idp
from provide.uterm.server.models import AuthConfig

_TEST_KEY = "uterm-test-secret-32-byte-minimum-key"  # pragma: allowlist secret


def _jwt_auth_config(key: str = _TEST_KEY) -> AuthConfig:
    return AuthConfig(
        mode="jwt",
        jwt_public_key_pem=key,
        jwt_algorithms=["HS256"],
        jwt_issuer="provide-uterm",
        jwt_audience="provide-uterm-server",
        worker_bearer_token="worker-secret-token-32-chars-long-x",  # pragma: allowlist secret
    )


def _make_token(sub: str = "alice", *, tenant: str | None = None, roles: list[str] | None = None) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "sub": sub,
        "roles": roles or ["operator"],
        "iss": "provide-uterm",
        "aud": "provide-uterm-server",
        "iat": now,
        "nbf": now,
        "exp": now + 600,
    }
    if tenant is not None:
        payload["tenant_id"] = tenant
    return jwt.encode(payload, key=_TEST_KEY, algorithm="HS256")


# ---------------------------------------------------------------------------
# canonical_tenant_id
# ---------------------------------------------------------------------------


class TestCanonicalTenantId:
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_blank_is_none(self, value: str | None) -> None:
        assert canonical_tenant_id(value) is None

    @pytest.mark.parametrize("value", ["bad tenant!", "-leading", "with/slash", "a" * 129])
    def test_invalid_is_none(self, value: str) -> None:
        assert canonical_tenant_id(value) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("acme", "acme"),
            ("  acme  ", "acme"),
            ("a.b-c_9", "a.b-c_9"),
            ("A", "A"),
            ("9", "9"),
            ("a" * 128, "a" * 128),
        ],
    )
    def test_valid_is_canonicalized(self, value: str, expected: str) -> None:
        assert canonical_tenant_id(value) == expected


# ---------------------------------------------------------------------------
# ApiKey record + tenant-scoped store methods
# ---------------------------------------------------------------------------


class TestTenantScopedStore:
    def test_flat_create_leaves_tenant_empty(self) -> None:
        store = ApiKeyStore()
        _raw, record = store.create("legacy")
        assert record.tenant_id == ""

    def test_api_key_dataclass_tenant_default(self) -> None:
        key = ApiKey(key_id="a", key_hash="b", name="c")
        assert key.tenant_id == ""

    def test_create_for_tenant_sets_canonical_tenant(self) -> None:
        store = ApiKeyStore()
        raw, record = store.create_for_tenant("  acme  ", "k", scopes=frozenset({"admin"}))
        assert record.tenant_id == "acme"
        assert store.validate(raw) is record

    def test_create_for_tenant_forwards_expiry(self) -> None:
        store = ApiKeyStore()
        before = time.time()
        _raw, record = store.create_for_tenant("acme", "temp", expires_in_s=3600)
        assert record.expires_at is not None
        assert record.expires_at >= before + 3600

    @pytest.mark.parametrize("bad", ["", "bad tenant!"])
    def test_create_for_tenant_rejects_invalid(self, bad: str) -> None:
        store = ApiKeyStore()
        with pytest.raises(ValueError, match="tenant_id is required"):
            store.create_for_tenant(bad, "k", scopes=frozenset({"admin"}))

    def test_list_keys_for_tenant_scopes_and_excludes_revoked(self) -> None:
        store = ApiKeyStore()
        _ra, rec_a = store.create_for_tenant("acme", "ka", scopes=frozenset({"admin"}))
        _rb, rec_b = store.create_for_tenant("beta", "kb", scopes=frozenset({"viewer"}))
        _rc, rec_c = store.create_for_tenant("acme", "kc", scopes=frozenset({"viewer"}))
        # A revoked acme key must drop out of the acme listing.
        store.revoke(rec_c.key_id)
        acme = store.list_keys_for_tenant("acme")
        assert [k.key_id for k in acme] == [rec_a.key_id]
        beta = store.list_keys_for_tenant("beta")
        assert [k.key_id for k in beta] == [rec_b.key_id]

    def test_list_keys_for_tenant_invalid_is_empty(self) -> None:
        store = ApiKeyStore()
        store.create_for_tenant("acme", "ka", scopes=frozenset({"admin"}))
        assert store.list_keys_for_tenant("bad tenant!") == []

    def test_revoke_for_tenant_own_tenant(self) -> None:
        store = ApiKeyStore()
        _raw, rec = store.create_for_tenant("acme", "ka", scopes=frozenset({"admin"}))
        assert store.revoke_for_tenant(rec.key_id, "acme") is True
        assert rec.revoked is True
        assert store.list_keys_for_tenant("acme") == []

    def test_revoke_for_tenant_cross_tenant_denied(self) -> None:
        store = ApiKeyStore()
        _raw, rec = store.create_for_tenant("acme", "ka", scopes=frozenset({"admin"}))
        assert store.revoke_for_tenant(rec.key_id, "beta") is False
        assert rec.revoked is False

    def test_revoke_for_tenant_invalid_tenant_denied(self) -> None:
        store = ApiKeyStore()
        _raw, rec = store.create_for_tenant("acme", "ka", scopes=frozenset({"admin"}))
        assert store.revoke_for_tenant(rec.key_id, "bad tenant!") is False
        assert rec.revoked is False

    def test_revoke_for_tenant_unknown_key_denied(self) -> None:
        store = ApiKeyStore()
        assert store.revoke_for_tenant("does-not-exist", "acme") is False


# ---------------------------------------------------------------------------
# API-key tenant resolution (fail-closed)
# ---------------------------------------------------------------------------


class TestApiKeyTenantResolution:
    def test_tenant_less_key_rejected(self) -> None:
        store = ApiKeyStore()
        raw_key, _record = store.create("legacy", scopes=frozenset({"admin"}))
        auth = AuthConfig(api_keys_enabled=True, mode="dev_token")
        assert _principal_from_api_key({"x-api-key": raw_key}, auth, store) is None

    def test_tenant_key_propagates_tenant(self) -> None:
        store = ApiKeyStore()
        raw_key, _record = store.create_for_tenant("acme", "scoped", scopes=frozenset({"operator"}))
        auth = AuthConfig(api_keys_enabled=True, mode="dev_token")
        principal = _principal_from_api_key({"x-api-key": raw_key}, auth, store)
        assert principal is not None
        assert principal.tenant_id == "acme"
        assert principal.roles == frozenset({"operator"})


# ---------------------------------------------------------------------------
# Header-mode tenant resolution
# ---------------------------------------------------------------------------


class TestHeaderTenantResolution:
    @staticmethod
    def _auth() -> AuthConfig:
        return AuthConfig(
            mode="header", worker_bearer_token="worker-secret-token-32-chars-long-x"
        )  # pragma: allowlist secret

    def test_valid_tenant_header(self) -> None:
        p = _principal_from_header_auth(
            {"x-uterm-principal": "alice", "x-uterm-role": "operator", "x-uterm-tenant": "acme"},
            {},
            self._auth(),
        )
        assert p.subject_id == "alice"
        assert p.tenant_id == "acme"

    def test_tenant_from_cookie(self) -> None:
        p = _principal_from_header_auth(
            {"x-uterm-principal": "alice"},
            {"uterm_tenant": "beta"},
            self._auth(),
        )
        assert p.tenant_id == "beta"

    def test_no_tenant_is_none(self) -> None:
        p = _principal_from_header_auth({"x-uterm-principal": "alice"}, {}, self._auth())
        assert p.tenant_id is None

    def test_invalid_tenant_fails_closed(self) -> None:
        p = _principal_from_header_auth(
            {"x-uterm-principal": "alice", "x-uterm-tenant": "bad tenant!"},
            {},
            self._auth(),
        )
        assert p.subject_id == "anonymous"
        assert p.tenant_id is None
        assert "viewer" in p.roles


# ---------------------------------------------------------------------------
# JWT-mode tenant resolution
# ---------------------------------------------------------------------------


class TestJwtTenantResolution:
    def test_valid_tenant_claim(self) -> None:
        auth = _jwt_auth_config()
        p = _principal_from_jwt_token(_make_token("alice", tenant="acme"), auth)
        assert p.tenant_id == "acme"

    def test_no_tenant_claim_is_none(self) -> None:
        auth = _jwt_auth_config()
        p = _principal_from_jwt_token(_make_token("alice", tenant=None), auth)
        assert p.tenant_id is None

    def test_invalid_tenant_claim_rejected(self) -> None:
        auth = _jwt_auth_config()
        with pytest.raises(ValueError, match="invalid tenant_id claim"):
            _principal_from_jwt_token(_make_token("alice", tenant="bad tenant!"), auth)


# ---------------------------------------------------------------------------
# Graphical.* capabilities
# ---------------------------------------------------------------------------


class TestGraphicalCapabilities:
    async def test_viewer_has_target_read_only(self) -> None:
        authz = AuthorizationService()
        p = Principal(subject_id="u", roles=frozenset({"viewer"}))
        caps = await authz.capabilities_for(p)
        assert "graphical.target.read" in caps
        assert "graphical.target.manage" not in caps
        assert "graphical.session.attach" not in caps

    @pytest.mark.parametrize("role", ["operator", "admin"])
    async def test_operator_and_admin_have_manage_and_attach(self, role: str) -> None:
        authz = AuthorizationService()
        p = Principal(subject_id="u", roles=frozenset({role}))
        caps = await authz.capabilities_for(p)
        assert "graphical.target.read" in caps
        assert "graphical.target.manage" in caps
        assert "graphical.session.attach" in caps

    def test_role_capabilities_literal(self) -> None:
        assert "graphical.target.read" in ROLE_CAPABILITIES["viewer"]
        assert "graphical.target.manage" not in ROLE_CAPABILITIES["viewer"]
        for role in ("operator", "admin"):
            assert {"graphical.target.read", "graphical.target.manage", "graphical.session.attach"} <= (
                ROLE_CAPABILITIES[role]
            )


# ---------------------------------------------------------------------------
# Config defaults + dev-IdP tenant claim
# ---------------------------------------------------------------------------


class TestTenantConfigAndDevIdp:
    def test_auth_config_tenant_defaults(self) -> None:
        auth = AuthConfig()
        assert auth.tenant_header == "x-uterm-tenant"
        assert auth.tenant_cookie == "uterm_tenant"
        assert auth.jwt_tenant_claim == "tenant_id"

    def test_principal_tenant_default_none(self) -> None:
        assert Principal(subject_id="u").tenant_id is None

    def test_dev_idp_mints_tenant_claim(self, tmp_path: Path) -> None:
        auth = AuthConfig(mode="dev_token")
        token = setup_dev_idp(
            auth,
            token_path=tmp_path / "dev_token",
            subject="dev",
            roles=("operator",),
            tenant="acme",
        )
        p = _principal_from_jwt_token(token, auth)
        assert p.tenant_id == "acme"

    def test_dev_idp_without_tenant_has_none(self, tmp_path: Path) -> None:
        auth = AuthConfig(mode="dev_token")
        token = setup_dev_idp(
            auth,
            token_path=tmp_path / "dev_token2",
            subject="dev",
            roles=("operator",),
        )
        p = _principal_from_jwt_token(token, auth)
        assert p.tenant_id is None
