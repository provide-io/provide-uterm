#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Property-based tests for auth placeholder/entropy helpers."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from provide.uterm.server.app.auth import (
    _MIN_BEARER_TOKEN_CHARS,
    _PLACEHOLDER_AUTH_MARKERS,
    _PLACEHOLDER_AUTH_VALUES,
    _is_low_entropy_bearer,
    _is_low_entropy_hmac_secret,
    _is_placeholder_auth_value,
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
