#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Gap #8: DeckMux owner/control-transfer via the SSH identity-frame path.

Verifies that PresenceStore keys and control-transfer payloads use real
subjects (e.g. ``"sre:alice"``) rather than an anonymous per-connection id when users
arrive through ``identity_as_principal(ResolvedIdentity(...))``.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from provide.uterm.auth import ResolvedIdentity
from provide.uterm.deckmux import identity_as_principal
from provide.uterm.deckmux._hub_mixin import DeckMuxMixin

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Test infrastructure (mirrors test_hub_mixin.py conventions)
# ---------------------------------------------------------------------------


class _FakeHub(DeckMuxMixin):
    """Minimal hub stub satisfying DeckMuxMixin's expectations."""

    def __init__(self) -> None:
        self._deckmux_init()
        self.broadcast = AsyncMock()


@dataclass
class _FakePrincipal:
    subject_id: str
    display_name: str = ""


class _FakeWS:
    """Fake websocket with a stable id for anonymous user_id derivation."""


# ---------------------------------------------------------------------------
# Test 1 — Real subjects land in the PresenceStore under their subject key
# ---------------------------------------------------------------------------


async def test_identity_principals_have_real_subject_keys() -> None:
    """Both alice and bob should appear in the PresenceStore keyed by their
    real subject strings, not by an anonymous per-connection id."""
    hub = _FakeHub()
    ws_alice = _FakeWS()
    ws_bob = _FakeWS()

    principal_alice = identity_as_principal(ResolvedIdentity(subject="sre:alice", claims={"display_name": "Alice"}))
    principal_bob = identity_as_principal(ResolvedIdentity(subject="sre:bob", claims={"display_name": "Bob"}))

    await hub.deckmux_on_browser_connect("w1", ws_alice, "operator", principal=principal_alice)
    result = await hub.deckmux_on_browser_connect("w1", ws_bob, "viewer", principal=principal_bob)

    store = hub._get_presence_store("w1")

    # Both real subjects must be resolvable in the store
    alice_entry = store.get("sre:alice")
    bob_entry = store.get("sre:bob")

    assert alice_entry is not None, "alice not found in presence store"
    assert bob_entry is not None, "bob not found in presence store"
    assert alice_entry.user_id == "sre:alice"
    assert bob_entry.user_id == "sre:bob"

    # Confirm they are NOT keyed by object-id fallback
    assert store.get(ws_alice._deckmux_anon_id) is None
    assert store.get(ws_bob._deckmux_anon_id) is None

    # The sync returned to bob must show both users
    assert result is not None
    user_ids_in_sync = {u["user_id"] for u in result["users"]}
    assert "sre:alice" in user_ids_in_sync
    assert "sre:bob" in user_ids_in_sync


# ---------------------------------------------------------------------------
# Test 2 — Control-request from bob is keyed by real subject
# ---------------------------------------------------------------------------


async def test_control_request_grants_to_real_subject() -> None:
    """When no owner exists and bob sends control_request, ownership is
    assigned to ``"sre:bob"`` (the real subject), not to an anonymous per-connection id.
    The hub broadcast carries a ``control_transfer`` with that subject as
    ``to_user_id``."""
    hub = _FakeHub()
    ws_alice = _FakeWS()
    ws_bob = _FakeWS()

    principal_alice = identity_as_principal(ResolvedIdentity(subject="sre:alice", claims={"display_name": "Alice"}))
    principal_bob = identity_as_principal(ResolvedIdentity(subject="sre:bob", claims={"display_name": "Bob"}))

    await hub.deckmux_on_browser_connect("w1", ws_alice, "operator", principal=principal_alice)
    await hub.deckmux_on_browser_connect("w1", ws_bob, "viewer", principal=principal_bob)
    hub.broadcast.reset_mock()

    # Bob requests control (no owner yet — should be granted immediately)
    await hub.deckmux_handle_message("w1", ws_bob, {"type": "control_request"}, principal=principal_bob)

    store = hub._get_presence_store("w1")
    owner = store.get_owner()

    assert owner is not None, "no owner was set after control_request"
    assert owner.user_id == "sre:bob", f"expected sre:bob as owner, got {owner.user_id!r}"

    # Broadcast should have been called with a control_transfer to sre:bob
    hub.broadcast.assert_called_once()
    _, msg = hub.broadcast.call_args[0]
    assert msg["type"] == "control_transfer"
    assert msg["to_user_id"] == "sre:bob", f"expected to_user_id=sre:bob, got {msg['to_user_id']!r}"
    assert msg["from_user_id"] == ""  # no previous owner


# ---------------------------------------------------------------------------
# Test 3 — Bob disconnects; alice persists; ownership cleared
# ---------------------------------------------------------------------------


async def test_disconnect_clears_owner_and_leaves_alice() -> None:
    """After bob (the owner) disconnects:
    - bob's presence entry is removed,
    - alice remains,
    - ownership is cleared (get_owner() returns None).

    Documented behaviour: the mixin does NOT auto-reassign to alice on
    disconnect; it simply clears the owner slot.
    """
    hub = _FakeHub()
    ws_alice = _FakeWS()
    ws_bob = _FakeWS()

    principal_alice = identity_as_principal(ResolvedIdentity(subject="sre:alice", claims={"display_name": "Alice"}))
    principal_bob = identity_as_principal(ResolvedIdentity(subject="sre:bob", claims={"display_name": "Bob"}))

    await hub.deckmux_on_browser_connect("w1", ws_alice, "operator", principal=principal_alice)
    await hub.deckmux_on_browser_connect("w1", ws_bob, "viewer", principal=principal_bob)

    # Bob acquires ownership
    await hub.deckmux_handle_message("w1", ws_bob, {"type": "control_request"}, principal=principal_bob)
    store = hub._get_presence_store("w1")
    assert store.get_owner() is not None and store.get_owner().user_id == "sre:bob"

    hub.broadcast.reset_mock()

    # Bob disconnects
    await hub.deckmux_on_browser_disconnect("w1", ws_bob, principal=principal_bob)

    # Bob's leave broadcast should have fired
    hub.broadcast.assert_called()
    leave_msgs = [call[0][1] for call in hub.broadcast.call_args_list if call[0][1].get("type") == "presence_leave"]
    assert leave_msgs, "no presence_leave broadcast after bob disconnected"
    assert leave_msgs[0]["user_id"] == "sre:bob"

    # Bob no longer in store; alice still there
    assert store.get("sre:bob") is None, "bob should be removed from store"
    assert store.get("sre:alice") is not None, "alice should still be in store"

    # Ownership is cleared (mixin does not auto-reassign)
    owner_after = store.get_owner()
    assert owner_after is None, f"expected no owner after bob disconnected, got {owner_after.user_id!r}"


# ---------------------------------------------------------------------------
# Test 4 — Regression guard: anonymous flow is unaffected
# ---------------------------------------------------------------------------


async def test_control_transfer_works_with_anonymous_principal() -> None:
    """Same control-request flow with ``principal=None`` (anonymous) must
    continue to function — confirming the identity-principal adapter doesn't
    break the anonymous code path."""
    hub = _FakeHub()
    ws = _FakeWS()

    # Anonymous connect + control_request
    await hub.deckmux_on_browser_connect("w1", ws, "admin")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})

    store = hub._get_presence_store("w1")
    owner = store.get_owner()
    assert owner is not None, "anonymous control_request should still grant ownership"
    # Anonymous user_id is a stable per-connection token (NOT str(id(ws))).
    anon_id = owner.user_id
    assert anon_id != str(id(ws))
    assert anon_id == ws._deckmux_anon_id
    assert anon_id  # non-empty

    hub.broadcast.assert_called_once()
    _, msg = hub.broadcast.call_args[0]
    assert msg["type"] == "control_transfer"
    # The broadcast must use the same per-connection token, consistently.
    assert msg["to_user_id"] == anon_id
    assert msg["from_user_id"] == ""
