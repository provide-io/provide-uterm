#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for server/auth.py."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import jwt

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
    from provide.uterm.server.models import AuthConfig

    return AuthConfig(
        mode="jwt",
        jwt_public_key_pem=key,
        jwt_algorithms=["HS256"],
        jwt_issuer="provide-uterm",
        jwt_audience="provide-uterm-server",
        worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
    )


def _header_auth_config():  # type: ignore[return]
    from provide.uterm.server.models import AuthConfig

    return AuthConfig(mode="header", worker_bearer_token=_make_token())


def _dev_auth_config():  # type: ignore[return]
    """Auth config for tests that need a header-driven principal flow."""
    from provide.uterm.server.models import AuthConfig

    return AuthConfig(mode="header", worker_bearer_token=_make_token())


# ---------------------------------------------------------------------------
# extract_bearer_token
# ---------------------------------------------------------------------------


class TestExtractBearerTokenMutations:
    def test_missing_authorization_key_returns_none(self) -> None:
        """mut_4: default=None causes str('None') to be non-empty."""
        from provide.uterm.server.auth import extract_bearer_token

        # Request with no authorization header should return None (not raise)
        result = extract_bearer_token({})
        assert result is None

    def test_split_on_space_not_none(self) -> None:
        """mut_12: split(None, 1) splits on any whitespace — must be ' '."""
        from provide.uterm.server.auth import extract_bearer_token

        # "Bearer\ttoken" — tab-split would work with None but not with " "
        result = extract_bearer_token({"authorization": "Bearer\ttoken"})
        # Should return None because split(" ", 1) doesn't split on tab
        assert result is None

    def test_default_empty_string_not_custom(self) -> None:
        """mut_9: default='XXXX' would make empty-auth detection fail."""
        from provide.uterm.server.auth import extract_bearer_token

        # Header present but only spaces — should return None
        result = extract_bearer_token({"authorization": "   "})
        assert result is None


# ---------------------------------------------------------------------------
# _roles_from_claims — list branch filtering
# ---------------------------------------------------------------------------


class TestRolesFromClaimsMutations:
    def _auth(self):  # type: ignore[return]
        from provide.uterm.server.models import AuthConfig

        return AuthConfig(mode="jwt", jwt_public_key_pem=_TEST_KEY, worker_bearer_token=_make_token())

    def test_list_filter_uses_actual_item_not_none(self) -> None:
        """mut_14: str(None).strip() would always produce 'None', filtering out all roles."""
        from provide.uterm.server.auth import _roles_from_claims

        # List with valid roles — should all be kept
        result = _roles_from_claims({"roles": ["viewer", "operator"]}, self._auth())
        assert "viewer" in result
        assert "operator" in result

    def test_list_empty_strings_filtered(self) -> None:
        """mut_14: with str(None), empty strings become 'None'."""
        from provide.uterm.server.auth import _roles_from_claims

        result = _roles_from_claims({"roles": ["", "admin", ""]}, self._auth())
        assert "admin" in result
        # Empty strings should not survive as valid roles
        assert "" not in result


# ---------------------------------------------------------------------------
# _anonymous_principal
# ---------------------------------------------------------------------------


class TestAnonymousPrincipalMutations:
    def test_scopes_is_frozenset_not_none(self) -> None:
        """mut_3: scopes=None."""
        from provide.uterm.server.auth import _anonymous_principal

        p = _anonymous_principal()
        assert p.scopes is not None
        assert isinstance(p.scopes, frozenset)

    def test_scopes_is_empty_frozenset(self) -> None:
        """mut_6: scopes omitted (missing kwarg) — uses dataclass default."""
        from provide.uterm.server.auth import _anonymous_principal

        p = _anonymous_principal()
        assert p.scopes == frozenset()

    def test_subject_is_anonymous(self) -> None:
        """verify subject_id is 'anonymous'."""
        from provide.uterm.server.auth import _anonymous_principal

        p = _anonymous_principal()
        assert p.subject_id == "anonymous"

    def test_roles_contains_viewer(self) -> None:
        from provide.uterm.server.auth import _anonymous_principal

        p = _anonymous_principal()
        assert "viewer" in p.roles


# ---------------------------------------------------------------------------
# _principal_from_header_auth mutations
# ---------------------------------------------------------------------------


class TestPrincipalFromHeaderAuthMutations:
    def test_scopes_is_empty_frozenset_not_none(self) -> None:
        """mut_33: scopes=None; mut_36: scopes omitted."""
        from provide.uterm.server.auth import _principal_from_header_auth

        p = _principal_from_header_auth({}, {}, _header_auth_config())
        assert p.scopes is not None
        assert isinstance(p.scopes, frozenset)
        assert p.scopes == frozenset()

    def test_missing_role_header_defaults_to_viewer(self) -> None:
        """mut_15/17/18: role_header default changed — missing role falls back to viewer."""
        from provide.uterm.server.auth import _principal_from_header_auth

        p = _principal_from_header_auth({"x-uterm-principal": "alice"}, {}, _header_auth_config())
        # No role header → falls back to viewer
        assert "viewer" in p.roles

    def test_viewer_role_accepted(self) -> None:
        """mut_22/23: 'viewer' removed/uppercased from valid set."""
        from provide.uterm.server.auth import _principal_from_header_auth

        p = _principal_from_header_auth(
            {"x-uterm-principal": "alice", "x-uterm-role": "viewer"}, {}, _header_auth_config()
        )
        assert "viewer" in p.roles

    def test_operator_role_accepted_in_header_mode(self) -> None:
        from provide.uterm.server.auth import _principal_from_header_auth

        p = _principal_from_header_auth(
            {"x-uterm-principal": "bob", "x-uterm-role": "operator"}, {}, _header_auth_config()
        )
        assert "operator" in p.roles

    def test_admin_role_accepted_in_header_mode(self) -> None:
        from provide.uterm.server.auth import _principal_from_header_auth

        p = _principal_from_header_auth(
            {"x-uterm-principal": "carol", "x-uterm-role": "admin"}, {}, _header_auth_config()
        )
        assert "admin" in p.roles


# ---------------------------------------------------------------------------
# _resolve_principal — JWT exception logging mutations
# ---------------------------------------------------------------------------


class TestResolvePrincipalMutations:
    def test_invalid_jwt_returns_anonymous_not_raises(self) -> None:
        """mut_41-44: logger.warning arg mutations — must still return anonymous."""
        from provide.uterm.server.auth import _resolve_principal

        auth = _jwt_auth_config()
        p = _resolve_principal({"authorization": "Bearer invalid.token.here"}, {}, auth, None)
        assert p.subject_id == "anonymous"

    def test_cookies_passed_to_header_auth_not_none(self) -> None:
        """mut_19: cookies=None in header mode path."""
        from provide.uterm.server.auth import _resolve_principal
        from provide.uterm.server.models import AuthConfig

        auth = AuthConfig(mode="header", worker_bearer_token=_make_token())
        # With cookies containing a principal, it should be accessible
        p = _resolve_principal({}, {"uterm_principal": "cookie_user"}, auth, None)
        assert p.subject_id == "cookie_user"


# ---------------------------------------------------------------------------
# resolve_http_principal / resolve_ws_principal
# ---------------------------------------------------------------------------


class TestResolvePrincipalPublicFunctions:
    def test_http_principal_no_headers_attr_uses_empty_dict(self) -> None:
        """mut_4/7: headers default=None or missing."""
        from provide.uterm.server.auth import resolve_http_principal

        # Request object with no headers attribute
        class _Req:
            pass

        auth = _dev_auth_config()
        # Should not raise — uses default {}
        p = resolve_http_principal(_Req(), auth)
        assert p is not None
        assert p.subject_id is not None

    def test_http_principal_with_headers_uses_them(self) -> None:
        """Verify headers attribute is actually used."""
        from provide.uterm.server.auth import resolve_http_principal

        class _Req:
            headers = {"x-uterm-principal": "req_user"}
            cookies: dict = {}

        auth = _dev_auth_config()
        p = resolve_http_principal(_Req(), auth)
        assert p.subject_id == "req_user"

    def test_ws_principal_no_headers_attr_uses_empty_dict(self) -> None:
        """mut_4/7: headers default=None or missing."""
        from provide.uterm.server.auth import resolve_ws_principal

        class _WS:
            pass

        auth = _dev_auth_config()
        p = resolve_ws_principal(_WS(), auth)
        assert p is not None
        assert p.subject_id is not None

    def test_ws_principal_with_cookies_attribute(self) -> None:
        """mut_11: cookies=None, mut_13: cookies default=None."""
        from provide.uterm.server.auth import resolve_ws_principal

        class _WS:
            headers: dict = {}
            cookies = {"uterm_principal": "ws_cookie_user"}

        auth = _dev_auth_config()
        p = resolve_ws_principal(_WS(), auth)
        assert p.subject_id == "ws_cookie_user"

    def test_ws_principal_without_cookies_attr(self) -> None:
        """No cookies attribute — should not raise."""
        from provide.uterm.server.auth import resolve_ws_principal

        class _WS:
            headers = {"x-uterm-principal": "ws_user"}

        auth = _dev_auth_config()
        p = resolve_ws_principal(_WS(), auth)
        assert p.subject_id == "ws_user"


# ---------------------------------------------------------------------------
# _resolve_jwt_key — JWKS client construction args
# ---------------------------------------------------------------------------


class TestResolveJwtKeyMutations:
    def test_jwks_client_created_with_url(self) -> None:
        """mut_1/11: url=None, mut_14: url kwarg missing."""
        from provide.uterm.server.auth import _JWKS_CLIENT_CACHE, _resolve_jwt_key
        from provide.uterm.server.models import AuthConfig

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

        # The first positional arg to PyJWKClient must be the URL
        call_args = mock_cls.call_args
        assert call_args is not None
        # Either positional or keyword arg
        if call_args.args:
            assert call_args.args[0] == "https://example.com/.well-known/jwks.json"
        else:
            assert call_args.kwargs.get("uri") == "https://example.com/.well-known/jwks.json"

        assert key == _TEST_KEY
        _JWKS_CLIENT_CACHE.clear()

    def test_jwks_client_cache_keys_true(self) -> None:
        """mut_12: cache_keys=None."""
        from provide.uterm.server.auth import _JWKS_CLIENT_CACHE, _resolve_jwt_key
        from provide.uterm.server.models import AuthConfig

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

        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs.get("cache_keys") is True
        _JWKS_CLIENT_CACHE.clear()

    def test_jwks_client_timeout_is_10(self) -> None:
        """mut_13: timeout=None."""
        from provide.uterm.server.auth import _JWKS_CLIENT_CACHE, _resolve_jwt_key
        from provide.uterm.server.models import AuthConfig

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

        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs.get("timeout") == 10
        _JWKS_CLIENT_CACHE.clear()


# ---------------------------------------------------------------------------
# resolve_principal — async wrapper must offload the (potentially blocking,
# JWKS-fetching) sync resolver to a worker thread via asyncio.to_thread so the
# event loop is never blocked on a network round-trip.
# ---------------------------------------------------------------------------


class TestResolvePrincipalOffloadsToThread:
    async def test_resolve_principal_runs_sync_off_the_event_loop(self) -> None:
        """Kills the mutant that drops asyncio.to_thread and calls
        resolve_principal_sync directly on the loop thread.

        We record the thread id observed inside the sync resolver; because the
        wrapper offloads via asyncio.to_thread, that id MUST differ from the
        event-loop thread id.
        """
        import asyncio
        import threading

        from provide.uterm.server.auth import LocalIdentityProvider
        from provide.uterm.server.bridge.identity import Principal

        loop_thread_id = threading.get_ident()
        # Sanity: confirm we really are on the running event loop's thread.
        assert asyncio.get_running_loop() is not None

        recorded: dict[str, int] = {}
        sentinel = Principal(subject_id="threaded", roles=frozenset({"viewer"}))

        idp = LocalIdentityProvider(_jwt_auth_config())

        def _record_thread(_connection: object) -> Principal:
            recorded["thread_id"] = threading.get_ident()
            return sentinel

        with patch.object(LocalIdentityProvider, "resolve_principal_sync", side_effect=_record_thread):
            result = await idp.resolve_principal(MagicMock())

        # The result is exactly what the sync resolver returned.
        assert result is sentinel
        # And the sync resolver ran on a *different* (worker) thread.
        assert recorded["thread_id"] != loop_thread_id

    async def test_resolve_principal_matches_sync_result(self) -> None:
        """The async wrapper must return the same Principal the sync entrypoint
        produces for a given connection (no semantic drift from the offload)."""
        from provide.uterm.server.auth import LocalIdentityProvider

        idp = LocalIdentityProvider(_jwt_auth_config())
        # An anonymous connection (no token) resolves deterministically.
        connection = MagicMock()
        connection.headers = {}
        connection.cookies = {}

        sync_principal = idp.resolve_principal_sync(connection)
        async_principal = await idp.resolve_principal(connection)

        assert async_principal.subject_id == sync_principal.subject_id
        assert async_principal.roles == sync_principal.roles
        assert async_principal.scopes == sync_principal.scopes
