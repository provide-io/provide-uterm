#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Property-based tests for auth placeholder/entropy helpers."""

from __future__ import annotations

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from provide.uterm.server.app.auth import (
    _MIN_BEARER_TOKEN_CHARS,
    _PLACEHOLDER_AUTH_MARKERS,
    _PLACEHOLDER_AUTH_VALUES,
    _is_low_entropy_bearer,
    _is_low_entropy_hmac_secret,
    _is_placeholder_auth_value,
)
from provide.uterm.server.auth import LocalIdentityProvider
from provide.uterm.server.authorization import AuthorizationService
from provide.uterm.server.bridge.identity import Principal, canonical_tenant_id
from provide.uterm.server.config_schema import AuthConfig

_SAFE_FIRST = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_SAFE_REST = _SAFE_FIRST + "_.-"
_safe_tenant_ids = st.builds(
    lambda first, rest: first + rest,
    st.sampled_from(tuple(_SAFE_FIRST)),
    st.text(alphabet=_SAFE_REST, max_size=127),
)


def test_placeholder_value_is_false_for_none_and_empty() -> None:
    assert _is_placeholder_auth_value(None) is False
    assert _is_placeholder_auth_value("") is False


@pytest.mark.parametrize("value", sorted(_PLACEHOLDER_AUTH_VALUES))
def test_placeholder_value_true_for_known_exact_values(value: str) -> None:
    assert _is_placeholder_auth_value(value) is True


@given(
    marker=st.sampled_from(_PLACEHOLDER_AUTH_MARKERS),
    prefix=st.text(max_size=8),
    suffix=st.text(max_size=8),
    upper=st.booleans(),
)
def test_placeholder_value_true_when_marker_substring_present(
    marker: str, prefix: str, suffix: str, upper: bool
) -> None:
    candidate = f"{prefix}{marker}{suffix}"
    if upper:
        candidate = candidate.upper()
    # The helper lowercases internally; constructed string is always non-empty
    # because ``marker`` itself is non-empty.
    assert _is_placeholder_auth_value(candidate) is True


@given(st.text())
def test_low_entropy_bearer_iff_nonempty_and_short(value: str) -> None:
    expected = 0 < len(value) < _MIN_BEARER_TOKEN_CHARS
    assert _is_low_entropy_bearer(value) is expected


def test_low_entropy_bearer_none_is_false() -> None:
    assert _is_low_entropy_bearer(None) is False


@given(
    value=st.text(min_size=1, max_size=10),
    algorithms=st.lists(
        st.sampled_from(["RS256", "ES256", "PS256", "EdDSA", "RS512"]),
        min_size=1,
        max_size=4,
    ),
)
def test_low_entropy_hmac_false_when_no_hs_algorithm(value: str, algorithms: list[str]) -> None:
    assert _is_low_entropy_hmac_secret(value, algorithms) is False


@given(suffix=st.text(max_size=20))
def test_low_entropy_hmac_false_for_pem_prefix(suffix: str) -> None:
    value = f"-----BEGIN{suffix}"
    assert _is_low_entropy_hmac_secret(value, ("HS256",)) is False


@given(value=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=32, max_size=200))
def test_low_entropy_hmac_false_for_long_hs256_secret(value: str) -> None:
    # Alphabet excludes whitespace and "-" so strip() and the PEM prefix
    # check cannot reduce the effective length below 32.
    assert _is_low_entropy_hmac_secret(value, ("HS256",)) is False


@given(
    value=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=31),
)
def test_low_entropy_hmac_true_for_short_hs256_secret(value: str) -> None:
    assert _is_low_entropy_hmac_secret(value, ("HS256",)) is True


@given(st.text(alphabet=" \t\n\r", max_size=10))
def test_low_entropy_hmac_empty_or_whitespace_is_false(value: str) -> None:
    assert _is_low_entropy_hmac_secret(value, ("HS256",)) is False


@given(_safe_tenant_ids)
def test_canonical_tenant_accepts_safe_ids_without_normalizing(value: str) -> None:
    assert canonical_tenant_id(value) == value


@given(
    st.one_of(
        st.text(alphabet="\x00\n\r\t", min_size=1),
        st.text(alphabet="租戶é😀", min_size=1),
        st.text(alphabet="a", min_size=129, max_size=140),
        st.sampled_from(["", "-tenant", ".tenant", " tenant", "tenant "]),
    )
)
def test_canonical_tenant_rejects_controls_unicode_and_boundaries(value: str) -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        canonical_tenant_id(value)


@given(header_tenant=_safe_tenant_ids, cookie_tenant=_safe_tenant_ids)
def test_header_tenant_precedence_property(header_tenant: str, cookie_tenant: str) -> None:
    principal = LocalIdentityProvider(AuthConfig(mode="header"))._principal_from_header_auth(
        {"x-uterm-principal": "user", "x-uterm-tenant": header_tenant},
        {"uterm_tenant": cookie_tenant},
    )
    assert principal.tenant_id == header_tenant


@given(principal_tenant=_safe_tenant_ids, target_tenant=_safe_tenant_ids)
@pytest.mark.asyncio
async def test_graphical_attach_cross_tenant_denial_property(principal_tenant: str, target_tenant: str) -> None:
    assume(principal_tenant != target_tenant)
    principal = Principal(
        subject_id="admin",
        tenant_id=principal_tenant,
        roles=frozenset({"admin"}),
    )
    assert await AuthorizationService().can_attach_graphical_session(principal, target_tenant) is False
