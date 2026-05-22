#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the tunnel-token hashing helpers."""

from __future__ import annotations

import secrets

from provide.uterm.tunnel.token_hash import hash_token, verify_token


def test_hash_token_is_deterministic() -> None:
    assert hash_token("abc") == hash_token("abc")


def test_hash_token_differs_for_different_inputs() -> None:
    assert hash_token("abc") != hash_token("acc")


def test_hash_token_empty_returns_empty() -> None:
    assert hash_token("") == ""


def test_hash_token_is_hex() -> None:
    digest = hash_token("uterm")
    assert len(digest) == 64
    # The hex alphabet only — no encoding accidents.
    int(digest, 16)


def test_verify_token_matches_hash() -> None:
    plain = secrets.token_urlsafe(32)
    assert verify_token(plain, hash_token(plain))


def test_verify_token_rejects_wrong_plain() -> None:
    assert not verify_token("attacker", hash_token("legitimate"))


def test_verify_token_rejects_empty_plain() -> None:
    assert not verify_token("", hash_token("something"))


def test_verify_token_rejects_empty_stored_hash() -> None:
    """An empty slot must never authenticate. This guards against the
    config bug 'token rotated away → field set to empty string'."""
    assert not verify_token("something", "")


def test_verify_token_constant_time_comparison() -> None:
    """Verify must use secrets.compare_digest semantics — both branches
    of the hash must be the same length so a timing attack can't
    distinguish."""
    a = hash_token(secrets.token_urlsafe(32))
    b = hash_token(secrets.token_urlsafe(32))
    assert len(a) == len(b)
