#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for server auth.py — principal resolution and JWT helpers."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest

_TEST_KEY = "uterm-test-secret-32-byte-minimum-key"


def _make_token(
    sub: str = "user1",
    roles: Any = None,
    exp_offset: int = 600,
    key: str = _TEST_KEY,
) -> str:
    if roles is None:
        roles = ["operator"]
    now = int(time.time())
    payload = {
        "sub": sub,
        "roles": roles,
        "iss": "provide-uterm",
        "aud": "provide-uterm-server",
        "iat": now,
        "nbf": now,
        "exp": now + exp_offset,
    }
    return jwt.encode(payload, key=key, algorithm="HS256")


def _jwt_auth_config(key: str = _TEST_KEY):  # type: ignore[return]
    from provide.terminal.server.models import AuthConfig

    return AuthConfig(
        mode="jwt",
        jwt_public_key_pem=key,
        jwt_algorithms=["HS256"],
        jwt_issuer="provide-uterm",
        jwt_audience="provide-uterm-server",
        worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
    )


class TestCookieValue:
    def test_missing_key_returns_none(self) -> None:
        from provide.terminal.server.auth import _cookie_value

        assert _cookie_value({}, "token") is None

    def test_whitespace_only_returns_none(self) -> None:
        from provide.terminal.server.auth import _cookie_value

        assert _cookie_value({"token": "   "}, "token") is None

    def test_present_value_returned(self) -> None:
        from provide.terminal.server.auth import _cookie_value

        assert _cookie_value({"token": "abc"}, "token") == "abc"


class TestExtractBearerToken:
    def test_empty_auth_returns_none(self) -> None:
        from provide.terminal.server.auth import extract_bearer_token

        assert extract_bearer_token({"authorization": ""}) is None

    def test_missing_auth_returns_none(self) -> None:
        from provide.terminal.server.auth import extract_bearer_token

        assert extract_bearer_token({}) is None

    def test_single_part_returns_none(self) -> None:
        from provide.terminal.server.auth import extract_bearer_token

        # "Bearertoken" — no space, so split gives one part
        assert extract_bearer_token({"authorization": "Bearertoken"}) is None

    def test_non_bearer_scheme_returns_none(self) -> None:
        from provide.terminal.server.auth import extract_bearer_token

        assert extract_bearer_token({"authorization": "Basic abc123"}) is None

    def test_empty_token_returns_none(self) -> None:
        from provide.terminal.server.auth import extract_bearer_token

        assert extract_bearer_token({"authorization": "Bearer   "}) is None

    def test_valid_bearer_returns_token(self) -> None:
        from provide.terminal.server.auth import extract_bearer_token

        assert extract_bearer_token({"authorization": "Bearer mytoken"}) == "mytoken"

    def test_bearer_case_insensitive(self) -> None:
        from provide.terminal.server.auth import extract_bearer_token

        assert extract_bearer_token({"authorization": "BEARER mytoken"}) == "mytoken"


class TestRolesFromClaims:
    def _auth(self):  # type: ignore[return]
        from provide.terminal.server.models import AuthConfig

        return AuthConfig(mode="jwt", jwt_public_key_pem=_TEST_KEY, worker_bearer_token=_make_token())

    def test_string_roles_parsed(self) -> None:
        from provide.terminal.server.auth import _roles_from_claims

        result = _roles_from_claims({"roles": "operator, admin"}, self._auth())
        assert "operator" in result
        assert "admin" in result

    def test_list_roles_parsed(self) -> None:
        from provide.terminal.server.auth import _roles_from_claims

        result = _roles_from_claims({"roles": ["viewer", "admin"]}, self._auth())
        assert "viewer" in result

    def test_missing_roles_falls_back_to_viewer(self) -> None:
        from provide.terminal.server.auth import _roles_from_claims

        result = _roles_from_claims({}, self._auth())
        assert result == frozenset({"viewer"})

    def test_unknown_roles_fall_back_to_viewer(self) -> None:
        from provide.terminal.server.auth import _roles_from_claims

        result = _roles_from_claims({"roles": ["superuser", "god"]}, self._auth())
        assert result == frozenset({"viewer"})

    def test_none_roles_falls_back_to_viewer(self) -> None:
        from provide.terminal.server.auth import _roles_from_claims

        result = _roles_from_claims({"roles": None}, self._auth())
        assert result == frozenset({"viewer"})


class TestScopesFromClaims:
    def _auth(self):  # type: ignore[return]
        from provide.terminal.server.models import AuthConfig

        return AuthConfig(mode="jwt", jwt_public_key_pem=_TEST_KEY, worker_bearer_token=_make_token())

    def test_string_scopes_parsed(self) -> None:
        from provide.terminal.server.auth import _scopes_from_claims

        # jwt_scopes_claim default is "scope" (singular)
        result = _scopes_from_claims({"scope": "read write"}, self._auth())
        assert "read" in result
        assert "write" in result

    def test_list_scopes_parsed(self) -> None:
        from provide.terminal.server.auth import _scopes_from_claims

        result = _scopes_from_claims({"scope": ["read", "write"]}, self._auth())
        assert "read" in result

    def test_missing_scopes_empty(self) -> None:
        from provide.terminal.server.auth import _scopes_from_claims

        result = _scopes_from_claims({}, self._auth())
        assert result == frozenset()


class TestResolveJwtKey:
    def test_returns_pem_key_when_configured(self) -> None:
        from provide.terminal.server.auth import _resolve_jwt_key

        auth = _jwt_auth_config()
        key = _resolve_jwt_key("any_token", auth)
        assert key == _TEST_KEY

    def test_raises_when_no_key_or_jwks(self) -> None:
        from provide.terminal.server.auth import _resolve_jwt_key
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="jwt", worker_bearer_token=_make_token())
        with pytest.raises(ValueError, match="jwt_public_key_pem or jwt_jwks_url"):
            _resolve_jwt_key("token", auth)

    def test_jwks_url_creates_client(self) -> None:
        from provide.terminal.server.auth import _JWKS_CLIENT_CACHE, _resolve_jwt_key
        from provide.terminal.server.models import AuthConfig

        _JWKS_CLIENT_CACHE.clear()

        mock_signing_key = MagicMock()
        mock_signing_key.key = _TEST_KEY
        mock_client = MagicMock()
        mock_client.get_signing_key_from_jwt.return_value = mock_signing_key

        auth = AuthConfig(
            mode="jwt",
            jwt_jwks_url="https://example.com/.well-known/jwks.json",
            worker_bearer_token=_make_token(),
        )

        with patch("jwt.PyJWKClient", return_value=mock_client) as mock_cls:
            key = _resolve_jwt_key("some_token", auth)
            assert key == _TEST_KEY
            assert mock_cls.call_count == 1

    def test_jwks_client_cached_on_second_call(self) -> None:
        from provide.terminal.server.auth import _JWKS_CLIENT_CACHE, _resolve_jwt_key
        from provide.terminal.server.models import AuthConfig

        _JWKS_CLIENT_CACHE.clear()

        mock_signing_key = MagicMock()
        mock_signing_key.key = _TEST_KEY
        mock_client = MagicMock()
        mock_client.get_signing_key_from_jwt.return_value = mock_signing_key

        auth = AuthConfig(
            mode="jwt",
            jwt_jwks_url="https://example.com/.well-known/jwks.json",
            worker_bearer_token=_make_token(),
        )

        with patch("jwt.PyJWKClient", return_value=mock_client) as mock_cls:
            _resolve_jwt_key("some_token", auth)
            _resolve_jwt_key("some_token", auth)
            assert mock_cls.call_count == 1  # second call uses cache

    def test_jwks_client_cache_cleared_when_full(self) -> None:
        from provide.terminal.server.auth import _JWKS_CLIENT_CACHE, _JWKS_CLIENT_CACHE_MAX, _resolve_jwt_key
        from provide.terminal.server.models import AuthConfig

        _JWKS_CLIENT_CACHE.clear()
        mock_signing_key = MagicMock()
        mock_signing_key.key = _TEST_KEY
        mock_client = MagicMock()
        mock_client.get_signing_key_from_jwt.return_value = mock_signing_key

        # Fill cache to max with dummy entries
        for i in range(_JWKS_CLIENT_CACHE_MAX):
            _JWKS_CLIENT_CACHE[f"https://dummy-{i}.example.com/jwks"] = mock_client

        auth = AuthConfig(
            mode="jwt",
            jwt_jwks_url="https://new.example.com/.well-known/jwks.json",
            worker_bearer_token=_make_token(),
        )

        with patch("jwt.PyJWKClient", return_value=mock_client):
            key = _resolve_jwt_key("some_token", auth)

        assert key == _TEST_KEY
        # Half-eviction: _JWKS_CLIENT_CACHE_MAX // 2 oldest entries removed, then new entry added.
        expected = _JWKS_CLIENT_CACHE_MAX - _JWKS_CLIENT_CACHE_MAX // 2 + 1
        assert len(_JWKS_CLIENT_CACHE) == expected
        _JWKS_CLIENT_CACHE.clear()


class TestPrincipalFromJwtToken:
    def test_valid_token_returns_principal(self) -> None:
        from provide.terminal.server.auth import _principal_from_jwt_token

        auth = _jwt_auth_config()
        token = _make_token(sub="alice", roles=["admin"])
        p = _principal_from_jwt_token(token, auth)
        assert p.subject_id == "alice"
        assert "admin" in p.roles

    def test_empty_sub_raises(self) -> None:
        from provide.terminal.server.auth import _principal_from_jwt_token

        auth = _jwt_auth_config()
        now = int(time.time())
        # sub is whitespace only — passes JWT require check but stripped to empty
        token = jwt.encode(
            {
                "sub": "   ",
                "iss": "provide-uterm",
                "aud": "provide-uterm-server",
                "iat": now,
                "exp": now + 600,
            },
            key=_TEST_KEY,
            algorithm="HS256",
        )
        with pytest.raises((ValueError, Exception)):
            _principal_from_jwt_token(token, auth)


class TestPrincipalFromHeaderAuth:
    def _auth(self):  # type: ignore[return]
        from provide.terminal.server.models import AuthConfig

        return AuthConfig(mode="header", worker_bearer_token=_make_token())

    def test_header_principal_and_role_resolved(self) -> None:
        from provide.terminal.server.auth import _principal_from_header_auth

        p = _principal_from_header_auth(
            {"x-uterm-principal": "bob", "x-uterm-role": "operator"},
            {},
            self._auth(),
        )
        assert p.subject_id == "bob"
        assert "operator" in p.roles

    def test_missing_headers_fall_back_to_anonymous_viewer(self) -> None:
        from provide.terminal.server.auth import _principal_from_header_auth

        p = _principal_from_header_auth({}, {}, self._auth())
        assert p.subject_id == "anonymous"
        assert "viewer" in p.roles

    def test_invalid_role_falls_back_to_viewer(self) -> None:
        from provide.terminal.server.auth import _principal_from_header_auth

        p = _principal_from_header_auth({"x-uterm-role": "superuser"}, {}, self._auth())
        assert "viewer" in p.roles

    def test_cookie_principal_used_when_no_header(self) -> None:
        from provide.terminal.server.auth import _principal_from_header_auth

        # principal_cookie default is "uterm_principal"
        p = _principal_from_header_auth({}, {"uterm_principal": "cookieuser"}, self._auth())
        assert p.subject_id == "cookieuser"


class TestResolvePrincipal:
    def test_dev_mode_returns_admin_scopes(self) -> None:
        from provide.terminal.server.auth import _resolve_principal
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="dev", worker_bearer_token=_make_token())
        p = _resolve_principal({}, {}, auth, None)
        assert "admin" in p.roles

    def test_none_mode_returns_admin_scopes(self) -> None:
        from provide.terminal.server.auth import _resolve_principal
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="none", worker_bearer_token=_make_token())
        p = _resolve_principal({}, {}, auth, None)
        assert "admin" in p.roles

    def test_header_mode_uses_header_auth(self) -> None:
        from provide.terminal.server.auth import _resolve_principal
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="header", worker_bearer_token=_make_token())
        p = _resolve_principal({"x-uterm-principal": "charlie", "x-uterm-role": "viewer"}, {}, auth, None)
        assert p.subject_id == "charlie"
        assert "viewer" in p.roles

    def test_unknown_mode_raises_value_error(self) -> None:
        from provide.terminal.server.auth import _resolve_principal
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="dev", worker_bearer_token=_make_token())
        auth.mode = "mystery_mode"  # type: ignore[assignment]
        with pytest.raises(ValueError, match="unknown auth mode"):
            _resolve_principal({}, {}, auth, None)

    def test_jwt_mode_valid_bearer_token(self) -> None:
        from provide.terminal.server.auth import _resolve_principal

        auth = _jwt_auth_config()
        token = _make_token(sub="david", roles=["operator"])
        p = _resolve_principal({"authorization": f"Bearer {token}"}, {}, auth, None)
        assert p.subject_id == "david"

    def test_jwt_mode_no_token_returns_anonymous(self) -> None:
        from provide.terminal.server.auth import _resolve_principal

        auth = _jwt_auth_config()
        p = _resolve_principal({}, {}, auth, None)
        assert p.subject_id == "anonymous"

    def test_jwt_mode_invalid_token_returns_anonymous(self) -> None:
        from provide.terminal.server.auth import _resolve_principal

        auth = _jwt_auth_config()
        p = _resolve_principal({"authorization": "Bearer notavalidtoken"}, {}, auth, None)
        assert p.subject_id == "anonymous"

    def test_jwt_mode_cookie_fallback(self) -> None:
        from provide.terminal.server.auth import _resolve_principal

        auth = _jwt_auth_config()
        # Use the token_cookie field name (default: "uterm_token")
        token = _make_token(sub="eve", roles=["viewer"])
        p = _resolve_principal({}, {"uterm_token": token}, auth, None)
        assert p.subject_id == "eve"
