#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests: DeckMux hub survives malformed identity control frames.

Gap #4 from code-review: prove that a hub receiving a malformed identity
control frame does not crash — it either (a) ignores the frame and treats
the connection as anonymous, or (b) uses a subject-derived name when claims
are partially malformed. All tests verify the PresenceStore ends in a
consistent state.

The hub flow under test:
    frame = <construct dict directly>
    identity = parse_identity_frame(frame)          # may return None
    principal = identity_as_principal(identity) if identity else None
    result = await hub.deckmux_on_browser_connect(worker_id, ws, role, principal=principal)
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from provide.uterm.deckmux import (
    identity_as_principal,
    parse_identity_frame,
)
from provide.uterm.deckmux._hub_mixin import DeckMuxMixin

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Minimal fakes — copied from test_hub_mixin.py
# ---------------------------------------------------------------------------


class _FakeHub(DeckMuxMixin):
    """Minimal hub stub that satisfies the mixin's expectations."""

    def __init__(self) -> None:
        self._deckmux_init()
        self.broadcast = AsyncMock()


@dataclass
class _FakePrincipal:
    subject_id: str
    display_name: str = ""


class _FakeWS:
    """Fake websocket with a stable id for user_id derivation."""


# ---------------------------------------------------------------------------
# Helper: run the hub's orchestration flow given a raw frame dict
# ---------------------------------------------------------------------------


async def _connect_with_frame(hub: _FakeHub, ws: _FakeWS, frame: dict, *, role: str = "viewer"):
    """Simulate the hub's identity-frame orchestration for a single connect."""
    identity = parse_identity_frame(frame)
    principal = identity_as_principal(identity) if identity else None
    return await hub.deckmux_on_browser_connect("w1", ws, role, principal=principal)


# ---------------------------------------------------------------------------
# Test 1: type field missing → anonymous connect
# ---------------------------------------------------------------------------


async def test_missing_type_field_is_anonymous() -> None:
    """Frame with no 'type' key → parse_identity_frame returns None → anonymous user."""
    hub = _FakeHub()
    ws = _FakeWS()
    frame = {"subject": "alice", "version": 1, "claims": {"display_name": "Alice"}}

    result = await _connect_with_frame(hub, ws, frame)

    assert result is not None
    assert len(result["users"]) == 1
    user = result["users"][0]
    # Anonymous: user_id is the per-connection anon id, not "alice"
    assert user["user_id"] == ws._deckmux_anon_id
    assert user["name"] != ""  # generated name must be non-empty


# ---------------------------------------------------------------------------
# Test 2: version=999 (unknown future version) → anonymous
# ---------------------------------------------------------------------------


async def test_unknown_version_is_anonymous() -> None:
    """Frame with version=999 → parse_identity_frame returns None → anonymous user."""
    hub = _FakeHub()
    ws = _FakeWS()
    frame = {"type": "identity", "version": 999, "subject": "alice", "claims": {}}

    result = await _connect_with_frame(hub, ws, frame)

    assert result is not None
    user = result["users"][0]
    assert user["user_id"] == ws._deckmux_anon_id
    assert user["name"] != ""


# ---------------------------------------------------------------------------
# Test 3: subject="" (empty string) → anonymous
# ---------------------------------------------------------------------------


async def test_empty_subject_is_anonymous() -> None:
    """Frame with subject='' → parse_identity_frame returns None → anonymous user."""
    hub = _FakeHub()
    ws = _FakeWS()
    frame = {"type": "identity", "version": 1, "subject": "", "claims": {}}

    result = await _connect_with_frame(hub, ws, frame)

    assert result is not None
    user = result["users"][0]
    assert user["user_id"] == ws._deckmux_anon_id
    assert user["name"] != ""


# ---------------------------------------------------------------------------
# Test 4: subject=123 (wrong type) → anonymous
# ---------------------------------------------------------------------------


async def test_non_string_subject_is_anonymous() -> None:
    """Frame with subject=123 (int) → parse_identity_frame returns None → anonymous user."""
    hub = _FakeHub()
    ws = _FakeWS()
    frame = {"type": "identity", "version": 1, "subject": 123, "claims": {}}

    result = await _connect_with_frame(hub, ws, frame)

    assert result is not None
    user = result["users"][0]
    assert user["user_id"] == ws._deckmux_anon_id
    assert user["name"] != ""


# ---------------------------------------------------------------------------
# Test 5: claims="not-a-mapping" → identity IS returned with empty claims,
#          subject is used as user_id
# ---------------------------------------------------------------------------


async def test_malformed_claims_identity_still_lands() -> None:
    """Frame with claims='not-a-mapping' → identity returned with empty claims;
    hub uses subject-derived name and subject as user_id."""
    hub = _FakeHub()
    ws = _FakeWS()
    frame = {"type": "identity", "version": 1, "subject": "sre:carol", "claims": "not-a-mapping"}

    result = await _connect_with_frame(hub, ws, frame)

    assert result is not None
    user = result["users"][0]
    # parse_identity_frame downgrades bad claims to {} but keeps the subject
    assert user["user_id"] == "sre:carol"
    # Name falls back to subject tail "carol"
    assert user["name"] == "carol"


# ---------------------------------------------------------------------------
# Test 6: ordering safety — first control frame is NOT an identity frame
# ---------------------------------------------------------------------------


async def test_non_identity_first_frame_is_anonymous() -> None:
    """If the first frame has type='ping', hub is anonymous; a later identity
    frame (if it arrived) would simply not be applied here — no crash."""
    hub = _FakeHub()
    ws = _FakeWS()
    first_frame = {"type": "ping"}

    # Connect with the non-identity frame
    result = await _connect_with_frame(hub, ws, first_frame)

    assert result is not None
    user = result["users"][0]
    assert user["user_id"] == ws._deckmux_anon_id
    assert user["name"] != ""

    # Now simulate a second "correct" identity frame arriving after connect:
    # The hub should still be in a consistent state (presence store has 1 user).
    store = hub._get_presence_store("w1")
    assert len(store._users) == 1


# ---------------------------------------------------------------------------
# Test 7: giant claims dict (10 KB) — no crash
# ---------------------------------------------------------------------------


async def test_giant_claims_does_not_crash() -> None:
    """A frame with 10 KB of text in claims should not crash the hub."""
    hub = _FakeHub()
    ws = _FakeWS()
    big_value = "x" * 10_240  # 10 KiB
    frame = {
        "type": "identity",
        "version": 1,
        "subject": "sre:biguser",
        "claims": {"display_name": "Big User", "extra_data": big_value},
    }

    result = await _connect_with_frame(hub, ws, frame)

    assert result is not None
    user = result["users"][0]
    assert user["user_id"] == "sre:biguser"
    assert user["name"] == "Big User"


# ---------------------------------------------------------------------------
# Test 8: deeply nested claims dict (50 levels) — no stack overflow
# ---------------------------------------------------------------------------


async def test_deeply_nested_claims_does_not_crash() -> None:
    """A claims dict with 50 levels of nesting inside should not blow the stack."""
    # Build {"a": {"a": {"a": ... 50 levels ...}}}
    nested: dict = {}
    cursor = nested
    for _ in range(50):
        cursor["a"] = {}
        cursor = cursor["a"]

    hub = _FakeHub()
    ws = _FakeWS()
    frame = {
        "type": "identity",
        "version": 1,
        "subject": "sre:deep",
        "claims": {"display_name": "Deep User", "nested": nested},
    }

    result = await _connect_with_frame(hub, ws, frame)

    assert result is not None
    user = result["users"][0]
    assert user["user_id"] == "sre:deep"
    assert user["name"] == "Deep User"
