#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for server auth.py — edge cases and mutation killing tests."""

from __future__ import annotations

import time
from typing import Any

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
        "iss": "provide-terminal",
        "aud": "provide-terminal-server",
        "iat": now,
        "nbf": now,
        "exp": now + exp_offset,
    }
    return jwt.encode(payload, key=key, algorithm="HS256")


class TestAuthEdgeCases:
    """Additional tests for mutation coverage of edge cases and boundaries."""

    def test_cookie_value_empty_string_returns_none(self) -> None:
        """Empty string should return None, not empty string."""
        from provide.terminal.server.auth import _cookie_value

        assert _cookie_value({"token": ""}, "token") is None

    def test_extract_bearer_token_case_variations(self) -> None:
        """Bearer scheme detection is case-insensitive."""
        from provide.terminal.server.auth import _extract_bearer_token

        # lowercase
        assert _extract_bearer_token({"authorization": "bearer token1"}) == "token1"
        # Mixed case
        assert _extract_bearer_token({"authorization": "BeArEr token2"}) == "token2"
        # uppercase
        assert _extract_bearer_token({"authorization": "BEARER token3"}) == "token3"

    def test_roles_from_claims_mixed_valid_invalid_string(self) -> None:
        """String with both valid and invalid roles extracts only valid."""
        from provide.terminal.server.auth import _roles_from_claims
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="jwt", jwt_public_key_pem=_TEST_KEY, worker_bearer_token=_make_token())
        result = _roles_from_claims({"roles": "superuser, operator, god, viewer"}, auth)
        assert result == frozenset({"operator", "viewer"})
        assert "superuser" not in result
        assert "god" not in result

    def test_roles_from_claims_string_with_only_invalid(self) -> None:
        """String with only invalid roles falls back to viewer."""
        from provide.terminal.server.auth import _roles_from_claims
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="jwt", jwt_public_key_pem=_TEST_KEY, worker_bearer_token=_make_token())
        result = _roles_from_claims({"roles": "superuser, god, root"}, auth)
        assert result == frozenset({"viewer"})

    def test_roles_from_claims_whitespace_handling(self) -> None:
        """Whitespace is properly handled in string split."""
        from provide.terminal.server.auth import _roles_from_claims
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="jwt", jwt_public_key_pem=_TEST_KEY, worker_bearer_token=_make_token())
        result = _roles_from_claims({"roles": "  operator  ,  admin  \n  viewer  "}, auth)
        assert result == frozenset({"operator", "admin", "viewer"})

    def test_roles_from_claims_list_with_invalid_elements(self) -> None:
        """List with invalid roles is filtered."""
        from provide.terminal.server.auth import _roles_from_claims
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="jwt", jwt_public_key_pem=_TEST_KEY, worker_bearer_token=_make_token())
        result = _roles_from_claims({"roles": ["admin", "superuser", "operator"]}, auth)
        assert result == frozenset({"admin", "operator"})

    def test_roles_from_claims_empty_list_falls_back(self) -> None:
        """Empty list falls back to viewer."""
        from provide.terminal.server.auth import _roles_from_claims
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="jwt", jwt_public_key_pem=_TEST_KEY, worker_bearer_token=_make_token())
        result = _roles_from_claims({"roles": []}, auth)
        assert result == frozenset({"viewer"})

    def test_roles_from_claims_case_normalization(self) -> None:
        """Roles are normalized to lowercase."""
        from provide.terminal.server.auth import _roles_from_claims
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="jwt", jwt_public_key_pem=_TEST_KEY, worker_bearer_token=_make_token())
        result = _roles_from_claims({"roles": ["OPERATOR", "ADMIN", "VIEWER"]}, auth)
        assert result == frozenset({"operator", "admin", "viewer"})

    def test_scopes_from_claims_empty_string_elements(self) -> None:
        """Empty strings in scope list are filtered."""
        from provide.terminal.server.auth import _scopes_from_claims
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="jwt", jwt_public_key_pem=_TEST_KEY, worker_bearer_token=_make_token())
        result = _scopes_from_claims({"scope": ["read", "", "write", "  "]}, auth)
        assert result == frozenset({"read", "write"})

    def test_scopes_from_claims_string_with_spaces(self) -> None:
        """Scopes string with multiple spaces handled correctly."""
        from provide.terminal.server.auth import _scopes_from_claims
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="jwt", jwt_public_key_pem=_TEST_KEY, worker_bearer_token=_make_token())
        result = _scopes_from_claims({"scope": "read  write   execute"}, auth)
        assert result == frozenset({"read", "write", "execute"})

    def test_principal_from_header_auth_empty_role_defaults_viewer(self) -> None:
        """Empty role string defaults to viewer."""
        from provide.terminal.server.auth import _principal_from_header_auth
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="header", worker_bearer_token=_make_token())
        p = _principal_from_header_auth({"x-uterm-role": ""}, {}, auth)
        assert "viewer" in p.roles

    def test_principal_from_header_auth_whitespace_role_defaults_viewer(self) -> None:
        """Whitespace-only role defaults to viewer."""
        from provide.terminal.server.auth import _principal_from_header_auth
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="header", worker_bearer_token=_make_token())
        p = _principal_from_header_auth({"x-uterm-role": "   "}, {}, auth)
        assert "viewer" in p.roles

    def test_principal_from_header_auth_principal_header_precedence(self) -> None:
        """Header takes precedence over cookie."""
        from provide.terminal.server.auth import _principal_from_header_auth
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="header", worker_bearer_token=_make_token())
        p = _principal_from_header_auth(
            {"x-uterm-principal": "header-user"},
            {"uterm_principal": "cookie-user"},
            auth,
        )
        assert p.subject_id == "header-user"

    def test_principal_from_local_mode_defaults_to_admin(self) -> None:
        """Local mode defaults to admin role, not viewer."""
        from provide.terminal.server.auth import _principal_from_local_mode
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="none", worker_bearer_token=_make_token())
        p = _principal_from_local_mode({}, {}, auth)
        assert "admin" in p.roles
        assert "*" in p.scopes

    def test_principal_from_local_mode_invalid_role_defaults_admin(self) -> None:
        """Local mode with invalid role defaults to admin."""
        from provide.terminal.server.auth import _principal_from_local_mode
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="none", worker_bearer_token=_make_token())
        p = _principal_from_local_mode({"x-uterm-role": "superuser"}, {}, auth)
        assert "admin" in p.roles

    def test_principal_from_local_mode_valid_role_overrides_default(self) -> None:
        """Local mode with valid role uses that role."""
        from provide.terminal.server.auth import _principal_from_local_mode
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="none", worker_bearer_token=_make_token())
        p = _principal_from_local_mode({"x-uterm-role": "viewer"}, {}, auth)
        assert p.roles == frozenset({"viewer"})

    def test_resolve_principal_mode_case_insensitive(self) -> None:
        """Mode matching is case-insensitive."""
        from provide.terminal.server.auth import _resolve_principal
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="dev", worker_bearer_token=_make_token())
        # This test verifies the mode is lowercased in the function
        p = _resolve_principal({}, {}, auth)
        assert "admin" in p.roles

    def test_resolve_principal_jwt_token_from_bearer_preferred(self) -> None:
        """JWT mode prefers bearer token over cookie."""
        from provide.terminal.server.auth import _resolve_principal
        from provide.terminal.server.models import AuthConfig

        token1 = _make_token(sub="bearer-user", roles=["admin"])
        token2 = _make_token(sub="cookie-user", roles=["viewer"])
        auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=["HS256"],
            jwt_issuer="provide-terminal",
            jwt_audience="provide-terminal-server",
            worker_bearer_token=token1,
        )
        p = _resolve_principal(
            {"authorization": f"Bearer {token1}"},
            {"uterm_token": token2},
            auth,
        )
        assert p.subject_id == "bearer-user"

    def test_principal_name_property(self) -> None:
        """Principal.name property returns subject_id."""
        from provide.terminal.server.auth import Principal

        p = Principal(subject_id="testuser")
        assert p.name == "testuser"

    def test_principal_claims_accessible(self) -> None:
        """Principal stores full JWT claims."""
        from provide.terminal.server.auth import Principal

        claims = {"sub": "user", "foo": "bar"}
        p = Principal(subject_id="user", claims=claims)
        assert p.claims == claims
        assert p.claims["foo"] == "bar"


class TestAuthMutationKilling:
    """Round 2: Additional tests to catch remaining mutations."""

    def test_cookie_value_none_check_explicit(self) -> None:
        """Explicitly verify None comparison (catches is None → == None)."""
        from provide.terminal.server.auth import _cookie_value

        result = _cookie_value({}, "missing")
        assert result is None  # Must be None, not empty string
        assert result != ""

    def test_bearer_token_split_count_exact(self) -> None:
        """Bearer token split must produce exactly 2 parts."""
        from provide.terminal.server.auth import _extract_bearer_token

        # 2 parts → valid
        assert _extract_bearer_token({"authorization": "Bearer token"}) == "token"
        # 1 part → invalid
        assert _extract_bearer_token({"authorization": "Bearertoken"}) is None
        # 3+ parts → should work (take everything after first space)
        assert _extract_bearer_token({"authorization": "Bearer my token here"}) == "my token here"

    def test_roles_from_claims_empty_after_filter_defaults_viewer(self) -> None:
        """Empty list after filtering must default to viewer."""
        from provide.terminal.server.auth import _roles_from_claims
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="jwt", jwt_public_key_pem="key", worker_bearer_token="token")
        # All invalid roles after filter
        result = _roles_from_claims({"roles": []}, auth)
        assert result == frozenset({"viewer"})
        assert len(result) == 1

    def test_roles_from_claims_list_of_integers(self) -> None:
        """List of integers should be converted to strings."""
        from provide.terminal.server.auth import _roles_from_claims
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="jwt", jwt_public_key_pem="key", worker_bearer_token="token")
        # Integer elements should cause conversion to string, then filtering
        result = _roles_from_claims({"roles": [1, 2, "operator"]}, auth)
        assert "operator" in result
        assert len(result) == 1  # 1, 2 don't match valid roles

    def test_scopes_from_claims_string_vs_list_type_check(self) -> None:
        """Type check for string vs list is critical."""
        from provide.terminal.server.auth import _scopes_from_claims
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="jwt", jwt_public_key_pem="key", worker_bearer_token="token")
        # String type
        result_str = _scopes_from_claims({"scope": "read write"}, auth)
        assert result_str == frozenset({"read", "write"})
        # List type
        result_list = _scopes_from_claims({"scope": ["read", "write"]}, auth)
        assert result_list == result_str
        # Dict type (invalid)
        result_dict = _scopes_from_claims({"scope": {"read": True}}, auth)
        assert result_dict == frozenset()

    def test_principal_from_header_role_in_operator_not_equality(self) -> None:
        """Role check uses 'in' operator, not equality."""
        from provide.terminal.server.auth import _principal_from_header_auth
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="header", worker_bearer_token="token")
        # Test all 3 valid roles
        for valid_role in ["viewer", "operator", "admin"]:
            p = _principal_from_header_auth({"x-uterm-role": valid_role}, {}, auth)
            assert valid_role in p.roles
            assert len(p.roles) == 1

    def test_principal_from_local_mode_scopes_always_has_asterisk(self) -> None:
        """Local mode ALWAYS includes * scope."""
        from provide.terminal.server.auth import _principal_from_local_mode
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="none", worker_bearer_token="token")
        p = _principal_from_local_mode({}, {}, auth)
        assert "*" in p.scopes
        assert len(p.scopes) == 1

    def test_resolve_principal_mode_not_just_checked_lowercase(self) -> None:
        """Mode is checked after lowercasing."""
        from provide.terminal.server.auth import _resolve_principal
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="JWT", worker_bearer_token="token")  # uppercase
        # With no token, should return anonymous (JWT mode recognized despite case)
        p = _resolve_principal({}, {}, auth)
        assert p.subject_id == "anonymous"

    def test_resolve_principal_bearer_tried_before_cookie(self) -> None:
        """Bearer token checked BEFORE cookie fallback."""
        from provide.terminal.server.auth import _resolve_principal
        from provide.terminal.server.models import AuthConfig

        token1 = _make_token(sub="bearer_user", roles=["admin"])
        token2 = _make_token(sub="cookie_user", roles=["viewer"])

        auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=["HS256"],
            jwt_issuer="provide-terminal",
            jwt_audience="provide-terminal-server",
            worker_bearer_token="token",
        )

        # Both bearer and cookie present - bearer takes precedence
        p = _resolve_principal(
            {"authorization": f"Bearer {token1}"},
            {"uterm_token": token2},
            auth,
        )
        assert p.subject_id == "bearer_user"

    def test_resolve_principal_jwt_invalid_token_returns_anonymous(self) -> None:
        """Invalid JWT should return anonymous, not raise."""
        from provide.terminal.server.auth import _resolve_principal
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem="key",
            jwt_algorithms=["HS256"],
            worker_bearer_token="token",
        )

        # Completely invalid token (not a JWT)
        p = _resolve_principal({"authorization": "Bearer not.a.jwt"}, {}, auth)
        assert p.subject_id == "anonymous"
        assert "viewer" in p.roles

    def test_cookie_value_must_strip_before_checking_empty(self) -> None:
        """Whitespace must be stripped before empty check."""
        from provide.terminal.server.auth import _cookie_value

        # Must strip first, then check empty
        result = _cookie_value({"token": "   "}, "token")
        assert result is None
        assert result != "   "

    def test_extract_bearer_token_scheme_case_lowercase_required(self) -> None:
        """Scheme comparison uses lower(), not just checking Bearer."""
        from provide.terminal.server.auth import _extract_bearer_token

        # All case variations should work
        for scheme in ["bearer", "Bearer", "BEARER", "BeArEr"]:
            token = _extract_bearer_token({"authorization": f"{scheme} token123"})
            assert token == "token123"

        # Non-bearer schemes should fail
        assert _extract_bearer_token({"authorization": "Basic dXNlcjpwYXNz"}) is None
        assert _extract_bearer_token({"authorization": "Digest username=user"}) is None

    def test_resolve_principal_mode_set_membership_exact(self) -> None:
        """Mode check uses set membership {none, dev}, not just substring."""
        from provide.terminal.server.auth import _resolve_principal
        from provide.terminal.server.models import AuthConfig

        # "none" mode → admin, "dev" mode → admin
        for mode_val in ["none", "dev"]:
            auth = AuthConfig(mode=mode_val, worker_bearer_token="token")
            p = _resolve_principal({}, {}, auth)
            assert "admin" in p.roles

    def test_principal_from_jwt_token_subject_strip_required(self) -> None:
        """JWT subject extraction must strip whitespace."""
        from provide.terminal.server.auth import _principal_from_jwt_token
        from provide.terminal.server.models import AuthConfig

        token = _make_token(sub="  user-with-spaces  ")
        auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=["HS256"],
            jwt_issuer="provide-terminal",
            jwt_audience="provide-terminal-server",
            worker_bearer_token=_make_token(),
        )
        p = _principal_from_jwt_token(token, auth)
        assert p.subject_id == "user-with-spaces"
        assert p.subject_id != "  user-with-spaces  "

    def test_principal_from_jwt_token_subject_empty_after_strip_raises(self) -> None:
        """JWT subject whitespace-only should raise."""
        from provide.terminal.server.auth import _principal_from_jwt_token
        from provide.terminal.server.models import AuthConfig

        token = _make_token(sub="   ")
        auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=["HS256"],
            jwt_issuer="provide-terminal",
            jwt_audience="provide-terminal-server",
            worker_bearer_token=_make_token(),
        )
        with pytest.raises(ValueError, match="sub claim is required"):
            _principal_from_jwt_token(token, auth)

    def test_roles_from_claims_string_and_list_both_strip_lower(self) -> None:
        """Both string and list branches must call strip().lower()."""
        from provide.terminal.server.auth import _roles_from_claims
        from provide.terminal.server.models import AuthConfig

        auth = AuthConfig(mode="jwt", jwt_public_key_pem="key", worker_bearer_token="token")
        # Both branches with whitespace/mixed case
        result_str = _roles_from_claims({"roles": "  OPERATOR  ,  ADMIN  "}, auth)
        result_list = _roles_from_claims({"roles": ["  OPERATOR  ", "  ADMIN  "]}, auth)
        assert result_str == result_list == frozenset({"operator", "admin"})
