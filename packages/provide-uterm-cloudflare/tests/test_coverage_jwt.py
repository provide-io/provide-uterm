#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for auth/jwt.py — internal helpers and decode_jwt edge cases."""

from __future__ import annotations

import time

import pytest
from provide.uterm.cloudflare.auth.jwt import (
    JwtValidationError,
    _apply_role_map,
    _b64url_decode,
    _check_audience,
    _check_exp,
    _check_issuer,
    _check_nbf,
    _extract_roles,
    _find_jwk,
    _parse_jwt_parts,
    _parse_roles_claim,
    _validate_claims,
    decode_jwt,
)
from provide.uterm.cloudflare.config import JwtConfig

# ---------------------------------------------------------------------------
# _b64url_decode (lines 50-53)
# ---------------------------------------------------------------------------


def test_b64url_decode_no_padding() -> None:
    """Decodes base64url with no padding needed (length % 4 == 0)."""
    # "dGVzdA==" is base64 for "test" — strip padding to get base64url
    result = _b64url_decode("dGVzdA")
    assert result == b"test"


def test_b64url_decode_with_padding_needed() -> None:
    """Decodes base64url that needs 1 or 2 padding chars."""
    # "YQ" encodes "a" (needs 2 padding chars)
    assert _b64url_decode("YQ") == b"a"
    # "YWI" encodes "ab" (needs 1 padding char)
    assert _b64url_decode("YWI") == b"ab"


def test_b64url_decode_already_padded() -> None:
    """Decodes base64url when length % 4 == 0 (no extra padding)."""
    assert _b64url_decode("dGVzdA==") == b"test"


# ---------------------------------------------------------------------------
# _parse_jwt_parts (lines 58-65)
# ---------------------------------------------------------------------------


def test_parse_jwt_parts_malformed_too_few() -> None:
    with pytest.raises(JwtValidationError, match="malformed JWT"):
        _parse_jwt_parts("only.two")


def test_parse_jwt_parts_malformed_too_many() -> None:
    with pytest.raises(JwtValidationError, match="malformed JWT"):
        _parse_jwt_parts("one.two.three.four")


def test_parse_jwt_parts_valid() -> None:
    """Valid 3-part JWT is parsed into header, payload, signature, signing_input."""
    import base64
    import json

    header_b64 = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(json.dumps({"sub": "u1"}).encode()).rstrip(b"=").decode()
    sig_b64 = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=").decode()
    token = f"{header_b64}.{payload_b64}.{sig_b64}"

    header, payload, signature, signing_input = _parse_jwt_parts(token)
    assert header == {"alg": "HS256"}
    assert payload == {"sub": "u1"}
    assert signature == b"fakesig"
    assert signing_input == f"{header_b64}.{payload_b64}".encode()


# ---------------------------------------------------------------------------
# _find_jwk (lines 101-112)
# ---------------------------------------------------------------------------


def test_find_jwk_no_keys() -> None:
    with pytest.raises(JwtValidationError, match="no keys"):
        _find_jwk({"keys": []}, kid="k1", alg="RS256")


def test_find_jwk_empty_keys_field() -> None:
    with pytest.raises(JwtValidationError, match="no keys"):
        _find_jwk({}, kid="k1", alg="RS256")


def test_find_jwk_kid_match() -> None:
    keys = {"keys": [{"kid": "k1", "alg": "RS256", "n": "abc"}, {"kid": "k2", "alg": "RS256"}]}
    result = _find_jwk(keys, kid="k1", alg="RS256")
    assert result["kid"] == "k1"


def test_find_jwk_kid_no_match() -> None:
    keys = {"keys": [{"kid": "k1", "alg": "RS256"}]}
    with pytest.raises(JwtValidationError, match="no matching key"):
        _find_jwk(keys, kid="k99", alg="RS256")


def test_find_jwk_no_kid_alg_match() -> None:
    """When kid is None, matches by algorithm."""
    keys = {"keys": [{"alg": "ES256"}, {"alg": "RS256", "n": "xyz"}]}
    result = _find_jwk(keys, kid=None, alg="RS256")
    assert result["alg"] == "RS256"


def test_find_jwk_no_kid_no_alg_returns_first() -> None:
    """When kid=None and alg=None, returns the first key."""
    keys = {"keys": [{"alg": "RS256", "n": "first"}, {"alg": "ES256"}]}
    result = _find_jwk(keys, kid=None, alg=None)
    assert result["n"] == "first"


def test_find_jwk_no_kid_key_alg_none_matches() -> None:
    """When kid=None and key has no alg field, it matches any alg."""
    keys = {"keys": [{"n": "noalg"}]}
    result = _find_jwk(keys, kid=None, alg="RS256")
    assert result["n"] == "noalg"


def test_find_jwk_no_kid_alg_mismatch() -> None:
    """When kid=None and no key matches the alg, raises."""
    keys = {"keys": [{"alg": "ES256"}]}
    with pytest.raises(JwtValidationError, match="no matching key"):
        _find_jwk(keys, kid=None, alg="RS256")


# ---------------------------------------------------------------------------
# _validate_claims / _check_exp / _check_nbf / _check_issuer / _check_audience
# (lines 159-195)
# ---------------------------------------------------------------------------


def test_check_exp_missing() -> None:
    with pytest.raises(JwtValidationError, match="missing exp"):
        _check_exp({}, now=time.time(), leeway=0)


def test_check_exp_expired() -> None:
    with pytest.raises(JwtValidationError, match="expired"):
        _check_exp({"exp": time.time() - 100}, now=time.time(), leeway=0)


def test_check_exp_within_leeway() -> None:
    """Token expired 5s ago but leeway is 30s — should pass."""
    now = time.time()
    _check_exp({"exp": now - 5}, now=now, leeway=30)  # no exception


def test_check_nbf_future() -> None:
    now = time.time()
    with pytest.raises(JwtValidationError, match="not yet valid"):
        _check_nbf({"nbf": now + 100}, now=now, leeway=0)


def test_check_nbf_within_leeway() -> None:
    now = time.time()
    _check_nbf({"nbf": now + 5}, now=now, leeway=30)  # no exception


def test_check_nbf_none_is_ok() -> None:
    _check_nbf({}, now=time.time(), leeway=0)  # no exception


def test_check_issuer_wrong() -> None:
    config = JwtConfig(issuer="https://expected.com")
    with pytest.raises(JwtValidationError, match="invalid issuer"):
        _check_issuer({"iss": "https://wrong.com"}, config)


def test_check_issuer_none_config_skips() -> None:
    config = JwtConfig(issuer=None)
    _check_issuer({"iss": "anything"}, config)  # no exception


def test_check_issuer_correct() -> None:
    config = JwtConfig(issuer="https://right.com")
    _check_issuer({"iss": "https://right.com"}, config)  # no exception


def test_check_audience_wrong_string() -> None:
    config = JwtConfig(audience="expected-aud")
    with pytest.raises(JwtValidationError, match="invalid audience"):
        _check_audience({"aud": "wrong-aud"}, config)


def test_check_audience_wrong_list() -> None:
    config = JwtConfig(audience="expected-aud")
    with pytest.raises(JwtValidationError, match="invalid audience"):
        _check_audience({"aud": ["other1", "other2"]}, config)


def test_check_audience_correct_string() -> None:
    config = JwtConfig(audience="my-aud")
    _check_audience({"aud": "my-aud"}, config)  # no exception


def test_check_audience_correct_list() -> None:
    config = JwtConfig(audience="my-aud")
    _check_audience({"aud": ["other", "my-aud"]}, config)  # no exception


def test_check_audience_no_config_skips() -> None:
    config = JwtConfig(audience=None)
    _check_audience({"aud": "anything"}, config)  # no exception


def test_validate_claims_calls_all_checks() -> None:
    """_validate_claims runs all four checks."""
    now = time.time()
    config = JwtConfig(issuer="iss", audience="aud", clock_skew_seconds=0)
    payload = {"exp": now + 600, "iss": "iss", "aud": "aud"}
    _validate_claims(payload, config)  # no exception


def test_validate_claims_negative_skew_clamped() -> None:
    """Negative clock_skew_seconds is clamped to 0."""
    now = time.time()
    config = JwtConfig(clock_skew_seconds=-10)
    payload = {"exp": now + 600}
    _validate_claims(payload, config)  # no exception


# ---------------------------------------------------------------------------
# _parse_roles_claim (lines 287-293)
# ---------------------------------------------------------------------------


def test_parse_roles_claim_string() -> None:
    assert _parse_roles_claim("admin, operator") == ("admin", "operator")


def test_parse_roles_claim_string_single() -> None:
    assert _parse_roles_claim("admin") == ("admin",)


def test_parse_roles_claim_list() -> None:
    assert _parse_roles_claim(["admin", "viewer"]) == ("admin", "viewer")


def test_parse_roles_claim_other_type() -> None:
    assert _parse_roles_claim(42) == ()
    assert _parse_roles_claim(None) == ()
    assert _parse_roles_claim(True) == ()


def test_parse_roles_claim_empty_string() -> None:
    assert _parse_roles_claim("") == ()


def test_parse_roles_claim_string_with_blanks() -> None:
    assert _parse_roles_claim("  ,  , admin ,  ") == ("admin",)


# ---------------------------------------------------------------------------
# _apply_role_map (lines 296-303)
# ---------------------------------------------------------------------------


def test_apply_role_map_with_mapping() -> None:
    config = JwtConfig(jwt_role_map={"eng": "admin", "ops": "operator"})
    result = _apply_role_map(("eng", "ops"), config)
    assert result == ("admin", "operator")


def test_apply_role_map_without_mapping() -> None:
    config = JwtConfig(jwt_role_map={})
    result = _apply_role_map(("admin",), config)
    assert result == ("admin",)


def test_apply_role_map_empty_roles_default() -> None:
    config = JwtConfig(jwt_role_map={}, jwt_default_role="viewer")
    result = _apply_role_map((), config)
    assert result == ("viewer",)


def test_apply_role_map_empty_roles_custom_default() -> None:
    config = JwtConfig(jwt_role_map={}, jwt_default_role="operator")
    result = _apply_role_map((), config)
    assert result == ("operator",)


def test_apply_role_map_passthrough_unknown() -> None:
    config = JwtConfig(jwt_role_map={"known": "admin"})
    result = _apply_role_map(("unknown",), config)
    assert result == ("unknown",)


# ---------------------------------------------------------------------------
# _extract_roles (lines 306-313)
# ---------------------------------------------------------------------------


def test_extract_roles_from_roles_claim() -> None:
    config = JwtConfig(jwt_roles_claim="roles")
    roles = _extract_roles({"roles": ["admin"]}, config)
    assert "admin" in roles


def test_extract_roles_fallback_to_scope() -> None:
    config = JwtConfig(jwt_roles_claim="roles", jwt_scopes_claim="scope")
    roles = _extract_roles({"scope": "read write"}, config)
    assert roles == ("read", "write")


def test_extract_roles_no_roles_no_scope_uses_default() -> None:
    config = JwtConfig(jwt_roles_claim="roles", jwt_default_role="viewer")
    roles = _extract_roles({}, config)
    assert roles == ("viewer",)


def test_extract_roles_scope_not_string_ignored() -> None:
    config = JwtConfig(jwt_roles_claim="roles", jwt_scopes_claim="scope", jwt_default_role="viewer")
    roles = _extract_roles({"scope": 123}, config)
    assert roles == ("viewer",)


def test_extract_roles_role_map_applied() -> None:
    config = JwtConfig(jwt_roles_claim="groups", jwt_role_map={"eng": "admin"})
    roles = _extract_roles({"groups": ["eng"]}, config)
    assert roles == ("admin",)


# ---------------------------------------------------------------------------
# decode_jwt — service token (sub="" with common_name fallback)
# ---------------------------------------------------------------------------


async def test_decode_jwt_service_token_common_name_fallback() -> None:
    """CF Access service token JWT: sub="" but common_name set → common_name subject.

    Admin is granted only when service-token admin is explicitly opted in.
    """
    import jwt as _jwt

    now = int(time.time())
    token = _jwt.encode(
        {"sub": "", "common_name": "my-service-client-id", "exp": now + 600},
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    config = JwtConfig(
        mode="jwt",
        public_key_pem="uterm-test-secret-32-byte-minimum-key",
        algorithms=("HS256",),
        jwt_service_token_admin=True,
    )
    principal = await decode_jwt(token, config)
    assert principal.subject_id == "my-service-client-id"
    assert principal.roles == ("admin",)


async def test_decode_jwt_empty_sub_no_common_name_raises() -> None:
    """sub="" and no common_name → missing sub error."""
    import jwt as _jwt

    now = int(time.time())
    token = _jwt.encode(
        {"sub": "", "exp": now + 600},
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    config = JwtConfig(mode="jwt", public_key_pem="uterm-test-secret-32-byte-minimum-key", algorithms=("HS256",))
    with pytest.raises(JwtValidationError, match="missing sub"):
        await decode_jwt(token, config)


async def test_decode_jwt_missing_public_key_raises() -> None:
    """No public_key_pem and no jwks_url → error."""
    config = JwtConfig(mode="jwt", public_key_pem=None, jwks_url=None)
    with pytest.raises(JwtValidationError, match="missing jwt public key"):
        await decode_jwt("some.token.here", config)


# ---------------------------------------------------------------------------
# _fetch_jwks (lines 72-97)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_jwks_urllib_timeout() -> None:
    """_fetch_jwks uses a 5s timeout for urllib.request.urlopen."""
    import json
    from unittest.mock import MagicMock, patch

    from provide.uterm.cloudflare.auth.jwt import _fetch_jwks

    url = "https://example.com/jwks-timeout-test-internal.json"
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"keys": []}).encode()
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        await _fetch_jwks(url)
        assert mock_urlopen.called
        _, kwargs = mock_urlopen.call_args
        assert kwargs.get("timeout") == 5


@pytest.mark.asyncio
async def test_fetch_jwks_rejects_non_http_scheme() -> None:
    """_fetch_jwks raises ValueError for non-http(s) schemes before opening the URL.

    The preflight scheme check turns bandit B310's URL-scheme concern into a real
    runtime gate; this test pins both branches of the rejection (file:// and a
    bare path) so a mutation that drops or weakens the check fails.
    """
    from provide.uterm.cloudflare.auth.jwt import _fetch_jwks

    for bad_url in ("file:///etc/passwd", "ftp://example.com/jwks.json", "/local/path"):
        with pytest.raises(ValueError, match="JWKS URL must be http"):
            await _fetch_jwks(bad_url)
