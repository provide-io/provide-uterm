#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for one-time tunnel invite helpers."""

from __future__ import annotations

from provide.uterm.server.tunnel_invites import (
    consume_tunnel_invite,
    discard_tunnel_invites_for_session,
    issue_tunnel_invites,
    sweep_expired_tunnel_invites,
    tunnel_invite_matches_token_hash,
)
from provide.uterm.tunnel.token_hash import hash_token


def test_issue_and_consume_tunnel_invite_success() -> None:
    store: dict[str, dict[str, object]] = {}
    share_invite, _control_invite = issue_tunnel_invites(
        store,
        session_id="s1",
        share_token="share-token",
        control_token="control-token",
        tunnel_expires_at=1_500.0,
        issued_ip="203.0.113.10",
        now=1_000.0,
    )

    consumed = consume_tunnel_invite(store, share_invite, session_id="s1", now=1_001.0)

    assert consumed is not None
    assert consumed.session_id == "s1"
    assert consumed.role == "viewer"
    assert consumed.tunnel_token == "share-token"
    assert consumed.issued_ip == "203.0.113.10"
    assert tunnel_invite_matches_token_hash(consumed, hash_token("share-token")) is True
    assert consume_tunnel_invite(store, share_invite, session_id="s1", now=1_001.0) is None


def test_consume_tunnel_invite_rejects_invalid_shapes() -> None:
    assert consume_tunnel_invite({}, "", session_id="s1") is None
    assert consume_tunnel_invite({}, "missing", session_id="s1") is None

    cases = [
        {"session_id": "s1", "role": "viewer", "tunnel_token": "tok", "expires_at": "bad"},
        {"session_id": "s1", "role": "viewer", "tunnel_token": "tok", "expires_at": 999.0},
        {"session_id": "other", "role": "viewer", "tunnel_token": "tok", "expires_at": 1_500.0},
        {"session_id": "s1", "role": "invalid", "tunnel_token": "tok", "expires_at": 1_500.0},
        {"session_id": "s1", "role": "viewer", "tunnel_token": "", "expires_at": 1_500.0},
    ]

    for index, raw in enumerate(cases):
        invite = f"invite-{index}"
        store = {hash_token(invite): raw}
        assert consume_tunnel_invite(store, invite, session_id="s1", now=1_000.0) is None


def test_discard_tunnel_invites_for_session_removes_only_matching_entries() -> None:
    store = {
        "a": {"session_id": "s1"},
        "b": {"session_id": "s2"},
        "c": {"session_id": "s1"},
    }

    discard_tunnel_invites_for_session(store, "s1")

    assert store == {"b": {"session_id": "s2"}}


def test_sweep_expired_tunnel_invites_removes_expired_numeric_entries() -> None:
    store = {
        "expired": {"expires_at": 999.0},
        "fresh": {"expires_at": 1_001.0},
        "unknown": {"expires_at": "not-a-number"},
    }

    sweep_expired_tunnel_invites(store, now=1_000.0)

    assert store == {
        "fresh": {"expires_at": 1_001.0},
        "unknown": {"expires_at": "not-a-number"},
    }
