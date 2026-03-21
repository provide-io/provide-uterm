#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for server/auth.py — JWT extraction, principals, and resolution."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.server.models import AuthConfig, RecordingConfig, SessionDefinition
from provide.terminal.server.registry import SessionRegistry
from provide.terminal.server.runtime import HostedSessionRuntime

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TEST_KEY = "uterm-test-secret-32-byte-minimum-key"


def _make_session(
    session_id: str = "test-session",
    connector_type: str = "shell",
    auto_start: bool = False,
    ephemeral: bool = False,
    owner: str | None = None,
    input_mode: str = "open",
    visibility: str = "public",
) -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        display_name="Test Session",
        connector_type=connector_type,
        auto_start=auto_start,
        ephemeral=ephemeral,
        owner=owner,
        input_mode=input_mode,  # type: ignore[arg-type]
        visibility=visibility,  # type: ignore[arg-type]
    )


def _make_runtime(
    session_id: str = "test-session",
    base_url: str = "http://localhost:9999",
    recording: RecordingConfig | None = None,
    worker_bearer_token: str | None = None,
) -> HostedSessionRuntime:
    return HostedSessionRuntime(
        _make_session(session_id),
        public_base_url=base_url,
        recording=recording or RecordingConfig(),
        worker_bearer_token=worker_bearer_token,
    )


def _make_hub() -> MagicMock:
    hub = MagicMock()
    hub.force_release_hijack = AsyncMock(return_value=True)
    hub.get_last_snapshot = AsyncMock(return_value=None)
    hub.get_recent_events = AsyncMock(return_value=[])
    hub.browser_count = AsyncMock(return_value=0)
    hub.on_worker_empty = None
    return hub


def _make_registry(
    sessions: list[SessionDefinition] | None = None,
    *,
    hub: MagicMock | None = None,
    recording: RecordingConfig | None = None,
    max_sessions: int | None = None,
) -> SessionRegistry:
    h = hub or _make_hub()
    return SessionRegistry(
        sessions or [],
        hub=h,
        public_base_url="http://localhost:9999",
        recording=recording or RecordingConfig(),
        max_sessions=max_sessions,
    )


def _jwt_auth_config(key: str = _TEST_KEY) -> AuthConfig:
    import jwt as pyjwt

    now = int(time.time())
    worker_token = pyjwt.encode(
        {"sub": "worker", "exp": now + 600, "iss": "provide-terminal", "aud": "provide-terminal-server"},
        key=key,
        algorithm="HS256",
    )
    return AuthConfig(
        mode="jwt",
        jwt_public_key_pem=key,
        jwt_algorithms=["HS256"],
        jwt_issuer="provide-terminal",
        jwt_audience="provide-terminal-server",
        worker_bearer_token=worker_token,
    )


def _make_jwt_token(
    sub: str = "user1",
    roles: Any = None,
    exp_offset: int = 600,
    key: str = _TEST_KEY,
) -> str:
    import jwt as pyjwt

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
    return pyjwt.encode(payload, key=key, algorithm="HS256")


# ===========================================================================
# runtime.py — HostedSessionRuntime.__init__
# ===========================================================================


class TestExtractBearerTokenMutants:
    def test_missing_authorization_returns_none(self) -> None:
        """mutmut_4/6: default None/nothing — str(None)='None' causes false match."""
        from provide.terminal.server.auth import extract_bearer_token

        # No authorization header at all
        result = extract_bearer_token({})
        assert result is None

    def test_non_empty_default_not_treated_as_bearer(self) -> None:
        """mutmut_9: default 'XXXX' would cause str check to fail differently."""
        from provide.terminal.server.auth import extract_bearer_token

        result = extract_bearer_token({})
        assert result is None

    def test_split_on_space_extracts_token(self) -> None:
        """mutmut_12: split(None, 1) splits on any whitespace (different semantics)."""
        from provide.terminal.server.auth import extract_bearer_token

        result = extract_bearer_token({"authorization": "Bearer   my-token"})
        # split(" ", 1) gives ["Bearer", "  my-token"]; strip() removes spaces
        # split(None, 1) gives ["Bearer", "my-token"] — slightly different but token valid
        # The key: make sure a token with multiple spaces is handled consistently
        assert result == "my-token"

    def test_split_none_difference_with_tab(self) -> None:
        """split(None,...) splits on tabs too — split(' ',1) does not."""
        from provide.terminal.server.auth import extract_bearer_token

        # Tab-separated header
        result = extract_bearer_token({"authorization": "Bearer\tmytoken"})
        # With split(" ", 1): only 1 part => None
        assert result is None


# ===========================================================================
# auth.py — _roles_from_claims()
# ===========================================================================


class TestRolesFromClaimsMutants:
    def _auth(self) -> AuthConfig:
        return AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            worker_bearer_token=_make_jwt_token(),
        )

    def test_list_roles_empty_string_filtered(self) -> None:
        """mutmut_14: str(None).strip() always truthy — empty strings would pass."""
        from provide.terminal.server.auth import _roles_from_claims

        # Empty string in list should be filtered (str("").strip() is falsy)
        result = _roles_from_claims({"roles": ["", "admin"]}, self._auth())
        assert "admin" in result

    def test_empty_string_in_list_not_included(self) -> None:
        """With the fix, empty strings are filtered before role validation."""
        from provide.terminal.server.auth import _roles_from_claims

        result = _roles_from_claims({"roles": [""]}, self._auth())
        # No valid role => fallback to viewer
        assert result == frozenset({"viewer"})


# ===========================================================================
# auth.py — _resolve_jwt_key()
# ===========================================================================


class TestResolveJwtKeyMutants:
    def test_public_key_pem_path_returned(self) -> None:
        """mutmut_22: error message changed (but raise still happens — testing positive path)."""
        from provide.terminal.server.auth import _resolve_jwt_key

        auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            worker_bearer_token=_make_jwt_token(),
        )
        key = _resolve_jwt_key("dummytoken", auth)
        assert key == _TEST_KEY

    def test_no_key_raises_value_error(self) -> None:
        """mutmut_22: error message mutated — still should raise ValueError."""
        from provide.terminal.server.auth import _resolve_jwt_key

        auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=None,
            jwt_jwks_url=None,
            worker_bearer_token=_make_jwt_token(),
        )
        with pytest.raises(ValueError):
            _resolve_jwt_key("token", auth)

    def test_jwks_cache_uses_correct_url(self) -> None:
        """mutmut_1: url = None instead of auth.jwt_jwks_url — cache key would be None."""
        from provide.terminal.server.auth import _JWKS_CLIENT_CACHE, _JWKS_CLIENT_CACHE_LOCK, _resolve_jwt_key

        auth = AuthConfig(
            mode="jwt",
            jwt_jwks_url="https://example.com/.well-known/jwks.json",
            worker_bearer_token=_make_jwt_token(),
        )
        # Clear the cache first
        with _JWKS_CLIENT_CACHE_LOCK:
            _JWKS_CLIENT_CACHE.clear()

        with patch("jwt.PyJWKClient") as mock_pyjwkclient:
            mock_client = MagicMock()
            mock_client.get_signing_key_from_jwt.return_value = MagicMock(key="mykey")
            mock_pyjwkclient.return_value = mock_client
            _resolve_jwt_key("sometoken", auth)

        # Verify the client was created with the correct URL
        call_args = mock_pyjwkclient.call_args[0]
        assert call_args[0] == "https://example.com/.well-known/jwks.json"

    def test_jwks_client_created_with_cache_keys_true(self) -> None:
        """mutmut_12/17: cache_keys=None or False instead of True."""
        from provide.terminal.server.auth import _JWKS_CLIENT_CACHE, _JWKS_CLIENT_CACHE_LOCK, _resolve_jwt_key

        auth = AuthConfig(
            mode="jwt",
            jwt_jwks_url="https://example.com/.well-known/jwks.json",
            worker_bearer_token=_make_jwt_token(),
        )
        with _JWKS_CLIENT_CACHE_LOCK:
            _JWKS_CLIENT_CACHE.clear()

        with patch("jwt.PyJWKClient") as mock_pyjwkclient:
            mock_client = MagicMock()
            mock_client.get_signing_key_from_jwt.return_value = MagicMock(key="k")
            mock_pyjwkclient.return_value = mock_client
            _resolve_jwt_key("sometoken", auth)

        call_kwargs = mock_pyjwkclient.call_args[1]
        assert call_kwargs.get("cache_keys") is True

    def test_jwks_client_created_with_timeout_10(self) -> None:
        """mutmut_13/16/18: timeout=None/omitted/11 instead of 10."""
        from provide.terminal.server.auth import _JWKS_CLIENT_CACHE, _JWKS_CLIENT_CACHE_LOCK, _resolve_jwt_key

        auth = AuthConfig(
            mode="jwt",
            jwt_jwks_url="https://example.com/.well-known/jwks.json",
            worker_bearer_token=_make_jwt_token(),
        )
        with _JWKS_CLIENT_CACHE_LOCK:
            _JWKS_CLIENT_CACHE.clear()

        with patch("jwt.PyJWKClient") as mock_pyjwkclient:
            mock_client = MagicMock()
            mock_client.get_signing_key_from_jwt.return_value = MagicMock(key="k")
            mock_pyjwkclient.return_value = mock_client
            _resolve_jwt_key("sometoken", auth)

        call_kwargs = mock_pyjwkclient.call_args[1]
        assert call_kwargs.get("timeout") == 10

    def test_get_signing_key_uses_token(self) -> None:
        """mutmut_20: get_signing_key_from_jwt(None) instead of (token)."""
        from provide.terminal.server.auth import _JWKS_CLIENT_CACHE, _JWKS_CLIENT_CACHE_LOCK, _resolve_jwt_key

        auth = AuthConfig(
            mode="jwt",
            jwt_jwks_url="https://example.com/.well-known/jwks.json",
            worker_bearer_token=_make_jwt_token(),
        )
        with _JWKS_CLIENT_CACHE_LOCK:
            _JWKS_CLIENT_CACHE.clear()

        the_token = "my.jwt.token"
        with patch("jwt.PyJWKClient") as mock_pyjwkclient:
            mock_client = MagicMock()
            mock_client.get_signing_key_from_jwt.return_value = MagicMock(key="k")
            mock_pyjwkclient.return_value = mock_client
            _resolve_jwt_key(the_token, auth)

        mock_client.get_signing_key_from_jwt.assert_called_with(the_token)


# ===========================================================================
# auth.py — _principal_from_jwt_token()
# ===========================================================================


class TestPrincipalFromJwtToken:
    def test_decodes_and_returns_principal(self) -> None:
        from provide.terminal.server.auth import _principal_from_jwt_token

        token = _make_jwt_token("user42", roles=["admin"])
        auth = _jwt_auth_config()
        p = _principal_from_jwt_token(token, auth)
        assert p.subject_id == "user42"
        assert "admin" in p.roles

    def test_leeway_zero_is_min(self) -> None:
        """mutmut_26: max(1,...) instead of max(0,...) — 0 second skew should give leeway=0."""
        from provide.terminal.server.auth import _principal_from_jwt_token

        auth = _jwt_auth_config()
        auth.clock_skew_seconds = 0  # type: ignore[assignment]
        token = _make_jwt_token()
        # Should not raise — leeway can be 0
        p = _principal_from_jwt_token(token, auth)
        assert p is not None

    def test_scopes_included_in_principal(self) -> None:
        """mutmut_49/53: scopes=None or scopes omitted."""
        import jwt as pyjwt

        from provide.terminal.server.auth import _principal_from_jwt_token

        auth = _jwt_auth_config()
        now = int(time.time())
        token = pyjwt.encode(
            {
                "sub": "user1",
                "roles": ["admin"],
                "scopes": "read write",
                "iss": "provide-terminal",
                "aud": "provide-terminal-server",
                "exp": now + 600,
            },
            key=_TEST_KEY,
            algorithm="HS256",
        )
        auth.jwt_scopes_claim = "scopes"  # type: ignore[assignment]
        p = _principal_from_jwt_token(token, auth)
        assert p.scopes is not None
        assert isinstance(p.scopes, frozenset)

    def test_claims_included_in_principal(self) -> None:
        """mutmut_50/54: claims=None or claims omitted."""
        from provide.terminal.server.auth import _principal_from_jwt_token

        token = _make_jwt_token("user1", roles=["operator"])
        auth = _jwt_auth_config()
        p = _principal_from_jwt_token(token, auth)
        assert p.claims is not None
        assert "sub" in p.claims
        assert p.claims["sub"] == "user1"

    def test_empty_sub_raises(self) -> None:
        """mutmut_45: error message changed — must still raise ValueError."""
        import jwt as pyjwt

        from provide.terminal.server.auth import _principal_from_jwt_token

        auth = _jwt_auth_config()
        now = int(time.time())
        # Token with empty sub
        token = pyjwt.encode(
            {
                "sub": "",
                "iss": "provide-terminal",
                "aud": "provide-terminal-server",
                "exp": now + 600,
            },
            key=_TEST_KEY,
            algorithm="HS256",
        )
        with pytest.raises(ValueError):
            _principal_from_jwt_token(token, auth)

    def test_resolve_jwt_key_called_with_token(self) -> None:
        """mutmut_2: _resolve_jwt_key(None, auth) instead of (token, auth)."""
        from provide.terminal.server.auth import _principal_from_jwt_token

        token = _make_jwt_token()
        auth = _jwt_auth_config()
        with patch("provide.terminal.server.auth._resolve_jwt_key") as mock_resolve:
            mock_resolve.return_value = _TEST_KEY
            _principal_from_jwt_token(token, auth)
        mock_resolve.assert_called_with(token, auth)
