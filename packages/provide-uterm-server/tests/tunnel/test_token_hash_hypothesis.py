#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Property-based tests for the tunnel-token hashing helpers."""

from __future__ import annotations

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from provide.uterm.tunnel.token_hash import hash_token, verify_token


@given(st.text(min_size=1))
def test_verify_round_trip_succeeds(plain: str) -> None:
    assert verify_token(plain, hash_token(plain)) is True


@given(st.text(min_size=1), st.text(min_size=1))
def test_verify_rejects_different_plain_tokens(a: str, b: str) -> None:
    assume(a != b)
    assert verify_token(a, hash_token(b)) is False


@given(st.text(min_size=1))
def test_hash_token_is_deterministic(plain: str) -> None:
    assert hash_token(plain) == hash_token(plain)


def test_hash_token_empty_returns_sentinel() -> None:
    assert hash_token("") == ""


@given(st.text(), st.text())
def test_verify_token_false_when_either_empty(plain: str, stored: str) -> None:
    assume(not plain or not stored)
    assert verify_token(plain, stored) is False


@given(st.text(min_size=1))
def test_hash_token_is_64_hex_chars(plain: str) -> None:
    digest = hash_token(plain)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


@given(st.text(min_size=1))
def test_verify_rejects_supplying_the_hash_as_plain(plain: str) -> None:
    # Defends against a caller who learns the stored digest and tries to
    # authenticate by submitting the digest itself as the plain token.
    stored = hash_token(plain)
    assume(plain != stored)
    assert verify_token(stored, stored) is False


@pytest.mark.parametrize("plain", ["a", "abc", "long" * 100, "é中\U0001f600"])
def test_known_round_trip_examples(plain: str) -> None:
    assert verify_token(plain, hash_token(plain)) is True
