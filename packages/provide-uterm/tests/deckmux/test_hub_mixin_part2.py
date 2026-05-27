#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for DeckMuxMixin — presence routing and control transfer."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from provide.uterm.deckmux._hub_mixin import DeckMuxMixin


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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_connect_sends_sync() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    result = await hub.deckmux_on_browser_connect("w1", ws, "operator")
    assert result is not None
    assert result["type"] == "presence_sync"
    assert len(result["users"]) == 1
    assert result["users"][0]["role"] == "operator"
    assert "config" in result


@pytest.mark.asyncio
async def test_browser_disconnect_broadcasts_leave() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "admin")
    hub.broadcast.reset_mock()

    await hub.deckmux_on_browser_disconnect("w1", ws)

    hub.broadcast.assert_called_once()
    args = hub.broadcast.call_args
    assert args[0][0] == "w1"
    assert args[0][1]["type"] == "presence_leave"
    assert args[0][1]["user_id"] == str(id(ws))


@pytest.mark.asyncio
async def test_browser_disconnect_no_broadcast_when_not_present() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    # Disconnect without connecting first
    await hub.deckmux_on_browser_disconnect("w1", ws)
    hub.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_presence_update_broadcast() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "operator")
    hub.broadcast.reset_mock()

    msg = {"type": "presence_update", "scroll_line": 42, "typing": True}
    await hub.deckmux_handle_message("w1", ws, msg)

    hub.broadcast.assert_called_once()
    broadcast_msg = hub.broadcast.call_args[0][1]
    assert broadcast_msg["type"] == "presence_update"
    assert broadcast_msg["scroll_line"] == 42
    assert broadcast_msg["typing"] is True


@pytest.mark.asyncio
async def test_presence_update_unknown_user_ignored() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    # Don't connect, just send an update
    msg = {"type": "presence_update", "scroll_line": 1}
    await hub.deckmux_handle_message("w1", ws, msg)
    hub.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_deckmux_handle_message_resolves_user_id_from_principal() -> None:
    """When principal carries subject_id, deckmux_handle_message uses it as
    the user_id so the presence store lookup matches the id set on connect.

    Regression guard for the branch at _hub_mixin.py:129 where the
    subject_id-based user_id resolution lives.  Without it, a JWT-auth
    deployment would fail presence_update lookups (the connect path uses
    subject_id but handle_message would fall back to id(ws)).
    """
    hub = _FakeHub()
    ws = _FakeWS()
    principal = _FakePrincipal(subject_id="user-abc", display_name="Alice")
    await hub.deckmux_on_browser_connect("w1", ws, "operator", principal=principal)
    hub.broadcast.reset_mock()

    msg = {"type": "presence_update", "scroll_line": 99}
    await hub.deckmux_handle_message("w1", ws, msg, principal=principal)

    hub.broadcast.assert_called_once()
    # The broadcast payload should carry the principal's subject_id, proving
    # the handler resolved user_id via the subject_id branch.
    broadcast_msg = hub.broadcast.call_args[0][1]
    assert broadcast_msg["user_id"] == "user-abc"


@pytest.mark.asyncio
async def test_queued_input_buffered() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "viewer")
    hub.broadcast.reset_mock()

    msg = {"type": "queued_input", "keys": "hello"}
    await hub.deckmux_handle_message("w1", ws, msg)

    hub.broadcast.assert_called_once()
    broadcast_msg = hub.broadcast.call_args[0][1]
    assert broadcast_msg["type"] == "presence_update"
    assert "hello" in broadcast_msg["queued_keys"]


@pytest.mark.asyncio
async def test_queued_input_unknown_user_ignored() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    msg = {"type": "queued_input", "keys": "x"}
    await hub.deckmux_handle_message("w1", ws, msg)
    hub.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_removes_state() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "viewer")
    # Force transfer manager creation
    hub._get_transfer_manager("w1")
    assert "w1" in hub._presence_stores
    assert "w1" in hub._transfer_managers

    hub.deckmux_cleanup("w1")

    assert "w1" not in hub._presence_stores
    assert "w1" not in hub._transfer_managers


@pytest.mark.asyncio
async def test_cleanup_idempotent() -> None:
    hub = _FakeHub()
    hub.deckmux_cleanup("nonexistent")
    # Should not raise


@pytest.mark.asyncio
async def test_identity_jwt_user() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    principal = _FakePrincipal(subject_id="user-123", display_name="Alice Smith")
    result = await hub.deckmux_on_browser_connect("w1", ws, "admin", principal=principal)

    assert result is not None
    user = result["users"][0]
    assert user["name"] == "Alice Smith"
    assert user["user_id"] == "user-123"
    assert user["initials"] == "AS"


@pytest.mark.asyncio
async def test_identity_jwt_user_no_display_name() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    principal = _FakePrincipal(subject_id="svc-456")
    result = await hub.deckmux_on_browser_connect("w1", ws, "operator", principal=principal)

    assert result is not None
    user = result["users"][0]
    # Falls back to subject_id as name
    assert user["name"] == "svc-456"
    assert user["user_id"] == "svc-456"


@pytest.mark.asyncio
async def test_identity_anonymous() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    result = await hub.deckmux_on_browser_connect("w1", ws, "viewer")

    assert result is not None
    user = result["users"][0]
    # Anonymous user gets a generated name
    assert user["name"] != ""
    assert user["user_id"] == str(id(ws))
    assert len(user["initials"]) == 2


@pytest.mark.asyncio
async def test_owner_typing_resets_warning() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "admin")

    # Make user the owner
    store = hub._get_presence_store("w1")
    user_id = str(id(ws))
    store.set_owner(user_id)

    tm = hub._get_transfer_manager("w1")
    tm._warning_sent = True

    msg = {"type": "presence_update", "typing": True}
    await hub.deckmux_handle_message("w1", ws, msg)

    assert tm._warning_sent is False


@pytest.mark.asyncio
async def test_control_request_grants_when_no_owner() -> None:
    """control_request grants control immediately when no owner exists."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "admin")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})

    hub.broadcast.assert_called_once()
    _, msg = hub.broadcast.call_args[0]
    assert msg["type"] == "control_transfer"
    assert msg["to_user_id"] == str(id(ws))
    assert msg["from_user_id"] == ""

    store = hub._get_presence_store("w1")
    owner = store.get_owner()
    assert owner is not None
    assert owner.user_id == str(id(ws))


@pytest.mark.asyncio
async def test_control_request_releases_when_already_owner() -> None:
    """control_request releases control when the requester already owns it."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "admin")
    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})

    hub.broadcast.assert_called_once()
    _, msg = hub.broadcast.call_args[0]
    assert msg["type"] == "control_transfer"
    assert msg["from_user_id"] == str(id(ws))
    assert msg["to_user_id"] == ""
    assert hub._get_presence_store("w1").get_owner() is None


@pytest.mark.asyncio
async def test_control_request_ignored_when_other_owns() -> None:
    """control_request is silently ignored when another user holds control."""
    hub = _FakeHub()
    ws_a = _FakeWS()
    ws_b = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws_a, "admin")
    await hub.deckmux_on_browser_connect("w1", ws_b, "admin")
    await hub.deckmux_handle_message("w1", ws_a, {"type": "control_request"})
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws_b, {"type": "control_request"})

    hub.broadcast.assert_not_called()
    owner = hub._get_presence_store("w1").get_owner()
    assert owner is not None
    assert owner.user_id == str(id(ws_a))


@pytest.mark.asyncio
async def test_multiple_browsers_sync() -> None:
    hub = _FakeHub()
    ws1 = _FakeWS()
    ws2 = _FakeWS()

    await hub.deckmux_on_browser_connect("w1", ws1, "admin")
    result = await hub.deckmux_on_browser_connect("w1", ws2, "viewer")

    assert result is not None
    assert len(result["users"]) == 2


@pytest.mark.asyncio
async def test_colors_avoid_collision() -> None:
    hub = _FakeHub()
    ws1 = _FakeWS()
    ws2 = _FakeWS()

    r1 = await hub.deckmux_on_browser_connect("w1", ws1, "admin")
    r2 = await hub.deckmux_on_browser_connect("w1", ws2, "viewer")

    assert r1 is not None
    assert r2 is not None
    c1 = r1["users"][0]["color"]
    c2 = r2["users"][1]["color"]
    # Colors should differ when possible
    assert c1 != c2


@pytest.mark.asyncio
async def test_presence_update_cols_rows_broadcast() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "operator")
    hub.broadcast.reset_mock()

    msg = {"type": "presence_update", "cols": 120, "rows": 40}
    await hub.deckmux_handle_message("w1", ws, msg)

    hub.broadcast.assert_called_once()
    broadcast_msg = hub.broadcast.call_args[0][1]
    assert broadcast_msg["cols"] == 120
    assert broadcast_msg["rows"] == 40


@pytest.mark.asyncio
async def test_unknown_message_type_ignored() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "operator")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "unknown_msg"})
    hub.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_connect_prunes_idle_users() -> None:
    """Stale users (idle > 60 s) are removed before sync is built for new connector."""
    import time

    hub = _FakeHub()
    ws_stale = _FakeWS()
    # Connect a user, then backdate their activity to make them appear idle
    await hub.deckmux_on_browser_connect("w1", ws_stale, "viewer")
    store = hub._get_presence_store("w1")
    stale_id = str(id(ws_stale))
    store._users[stale_id].last_activity_at = time.time() - 60  # 60 s ago (> 30 s threshold)

    # New user connects — stale user should be pruned from the sync
    ws_new = _FakeWS()
    result = await hub.deckmux_on_browser_connect("w1", ws_new, "operator")
    assert result is not None
    user_ids = [u["user_id"] for u in result["users"]]
    assert stale_id not in user_ids
    assert str(id(ws_new)) in user_ids


@pytest.mark.asyncio
async def test_transfer_manager_config() -> None:
    hub = _FakeHub()
    tm = hub._get_transfer_manager("w1", {"auto_transfer_idle_s": 60, "keystroke_queue": "replay"})
    assert tm._auto_idle_s == 60
    assert tm.queue_mode == "replay"

    # Calling again returns the same instance
    tm2 = hub._get_transfer_manager("w1")
    assert tm2 is tm


# ---------------------------------------------------------------------------
# Mutation killers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_grant_transfer_manager_keyed_by_worker_id() -> None:
    """Transfer manager for control_request grant is stored under worker_id, not None."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "admin")
    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})

    assert "w1" in hub._transfer_managers
    assert None not in hub._transfer_managers


def test_transfer_manager_empty_config_uses_defaults() -> None:
    """Empty config dict falls through to defaults (30, 'display')."""
    hub = _FakeHub()
    tm = hub._get_transfer_manager("w1", {})
    assert tm._auto_idle_s == 30
    assert tm.queue_mode == "display"


@pytest.mark.asyncio
async def test_presence_update_broadcast_uses_worker_id_not_none() -> None:
    """broadcast is called with the actual worker_id, not None."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "operator")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "presence_update", "scroll_line": 5})

    worker_id_arg = hub.broadcast.call_args[0][0]
    assert worker_id_arg == "w1"
    assert worker_id_arg is not None


@pytest.mark.asyncio
async def test_presence_update_scroll_range_forwarded() -> None:
    """scroll_range field is included in the broadcast (key name is 'scroll_range')."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "operator")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "presence_update", "scroll_range": [5, 29]})

    broadcast_msg = hub.broadcast.call_args[0][1]
    assert list(broadcast_msg.get("scroll_range", [])) == [5, 29]


@pytest.mark.asyncio
async def test_presence_update_selection_forwarded() -> None:
    """selection field is included in the broadcast (key name is 'selection')."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "operator")
    hub.broadcast.reset_mock()

    sel = {"start": 10, "end": 20}
    await hub.deckmux_handle_message("w1", ws, {"type": "presence_update", "selection": sel})

    broadcast_msg = hub.broadcast.call_args[0][1]
    assert broadcast_msg.get("selection") == sel


@pytest.mark.asyncio
async def test_presence_update_pin_forwarded() -> None:
    """pin field is included in the broadcast (key name is 'pin')."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "operator")
    hub.broadcast.reset_mock()

    pin = {"line": 42}
    await hub.deckmux_handle_message("w1", ws, {"type": "presence_update", "pin": pin})

    broadcast_msg = hub.broadcast.call_args[0][1]
    assert broadcast_msg.get("pin") == pin


@pytest.mark.asyncio
async def test_non_owner_typing_does_not_reset_warning() -> None:
    """Only the owner typing resets the warning — non-owner should NOT reset it."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "viewer")

    tm = hub._get_transfer_manager("w1")
    tm._warning_sent = True

    msg = {"type": "presence_update", "typing": True}
    await hub.deckmux_handle_message("w1", ws, msg)

    assert tm._warning_sent is True  # NOT reset by non-owner typing


@pytest.mark.asyncio
async def test_queued_input_missing_keys_field_defaults_to_empty() -> None:
    """queued_input without 'keys' key uses '' default, not None or 'XXXX'."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "viewer")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "queued_input"})

    hub.broadcast.assert_called_once()
    broadcast_msg = hub.broadcast.call_args[0][1]
    assert broadcast_msg.get("queued_keys") == ""


@pytest.mark.asyncio
async def test_queued_input_broadcast_uses_worker_id_not_none() -> None:
    """queued_input broadcast uses actual worker_id, not None."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "viewer")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "queued_input", "keys": "a"})

    worker_id_arg = hub.broadcast.call_args[0][0]
    assert worker_id_arg == "w1"
    assert worker_id_arg is not None


@pytest.mark.asyncio
async def test_queued_input_transfer_manager_keyed_by_worker_id() -> None:
    """Transfer manager for queued_input is stored under worker_id, not None."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "viewer")
    await hub.deckmux_handle_message("w1", ws, {"type": "queued_input", "keys": "abc"})

    assert "w1" in hub._transfer_managers
    assert None not in hub._transfer_managers


@pytest.mark.asyncio
async def test_queued_input_keys_isolated_per_user() -> None:
    """Each user's keystrokes accumulate independently — not under shared None key."""
    hub = _FakeHub()
    ws_a = _FakeWS()
    ws_b = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws_a, "viewer")
    await hub.deckmux_on_browser_connect("w1", ws_b, "viewer")

    await hub.deckmux_handle_message("w1", ws_a, {"type": "queued_input", "keys": "abc"})
    hub.broadcast.reset_mock()
    await hub.deckmux_handle_message("w1", ws_b, {"type": "queued_input", "keys": "xyz"})

    broadcast_msg = hub.broadcast.call_args[0][1]
    assert broadcast_msg["queued_keys"] == "xyz"  # not "abcxyz" (would merge if keyed by None)


@pytest.mark.asyncio
async def test_control_grant_reason_is_handover() -> None:
    """control_request grant sends reason='handover', not 'HANDOVER'/'XXhandoverXX'/None."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "admin")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})

    _, msg = hub.broadcast.call_args[0]
    assert msg["reason"] == "handover"


@pytest.mark.asyncio
async def test_control_release_reason_is_handover() -> None:
    """control_request release (owner releasing) sends reason='handover'."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "admin")
    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})

    _, msg = hub.broadcast.call_args[0]
    assert msg["reason"] == "handover"


@pytest.mark.asyncio
async def test_control_grant_broadcast_uses_worker_id_not_none() -> None:
    """control_request grant broadcasts to worker_id, not None."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "admin")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})

    worker_id_arg = hub.broadcast.call_args[0][0]
    assert worker_id_arg == "w1"
    assert worker_id_arg is not None


@pytest.mark.asyncio
async def test_control_release_broadcast_uses_worker_id_not_none() -> None:
    """control_request release broadcasts to worker_id, not None."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "admin")
    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})

    worker_id_arg = hub.broadcast.call_args[0][0]
    assert worker_id_arg == "w1"
    assert worker_id_arg is not None


@pytest.mark.asyncio
async def test_connect_sync_config_auto_transfer_idle_s_is_30() -> None:
    """Sync config auto_transfer_idle_s is 30, not 31 or None."""
    hub = _FakeHub()
    ws = _FakeWS()
    result = await hub.deckmux_on_browser_connect("w1", ws, "viewer")

    assert result is not None
    config = result.get("config")
    assert isinstance(config, dict)
    assert config["auto_transfer_idle_s"] == 30


@pytest.mark.asyncio
async def test_connect_sync_config_keystroke_queue_is_display() -> None:
    """Sync config keystroke_queue is 'display', not 'DISPLAY'/'XXdisplayXX'."""
    hub = _FakeHub()
    ws = _FakeWS()
    result = await hub.deckmux_on_browser_connect("w1", ws, "viewer")

    assert result is not None
    config = result.get("config")
    assert isinstance(config, dict)
    assert config["keystroke_queue"] == "display"


@pytest.mark.asyncio
async def test_connect_prunes_at_30_not_31_seconds() -> None:
    """prune_idle threshold is 30.0: a user idle 30.5s IS pruned (31.0 would NOT prune)."""
    import time

    hub = _FakeHub()
    ws_stale = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws_stale, "viewer")
    store = hub._get_presence_store("w1")
    stale_id = str(id(ws_stale))
    store._users[stale_id].last_activity_at = time.time() - 30.5  # between 30 and 31

    ws_new = _FakeWS()
    result = await hub.deckmux_on_browser_connect("w1", ws_new, "operator")
    assert result is not None
    user_ids = [u["user_id"] for u in result["users"]]
    assert stale_id not in user_ids  # pruned at 30.0; would NOT be pruned at 31.0


@pytest.mark.asyncio
async def test_principal_truthy_without_subject_id_uses_generated_name() -> None:
    """'principal and hasattr' — truthy principal lacking subject_id falls to else branch."""

    class _TruthyNoSubject:
        pass

    hub = _FakeHub()
    ws = _FakeWS()
    result = await hub.deckmux_on_browser_connect("w1", ws, "viewer", principal=_TruthyNoSubject())
    assert result is not None
    user = result["users"][0]
    # Correct: hasattr fails → generate_name → non-empty generated name
    # Mutation (or): truthy short-circuits → getattr falls back → name=""
    assert user["name"] != ""
    assert user["user_id"] == str(id(ws))


@pytest.mark.asyncio
async def test_principal_no_display_name_attr_falls_back_to_subject_id() -> None:
    """getattr default '' for missing display_name → '' or subject_id → subject_id."""

    class _PrincipalNoDisplayName:
        def __init__(self, subject_id: str) -> None:
            self.subject_id = subject_id

    hub = _FakeHub()
    ws = _FakeWS()
    result = await hub.deckmux_on_browser_connect("w1", ws, "admin", principal=_PrincipalNoDisplayName("svc-abc"))
    assert result is not None
    user = result["users"][0]
    # default="" → "" or "svc-abc" → "svc-abc"; mutation "XXXX": "XXXX" or ... → "XXXX"
    assert user["name"] == "svc-abc"


@pytest.mark.asyncio
async def test_colors_avoid_collision_with_taken_colors() -> None:
    """generate_color is called with store.taken_colors() — prevents same-index collision."""
    from provide.uterm.deckmux._names import _COLORS, _hash_int

    # Find two subject_ids that hash to the same default color index
    ids_by_idx: dict[int, list[str]] = {}
    for i in range(500):
        sid = f"col-{i}"
        h = _hash_int(sid)
        idx = h % len(_COLORS)
        ids_by_idx.setdefault(idx, []).append(sid)

    collision_pair = next((ids[:2] for ids in ids_by_idx.values() if len(ids) >= 2), None)
    assert collision_pair is not None, "No collision found among 500 IDs"
    id1, id2 = collision_pair

    hub = _FakeHub()
    ws1, ws2 = _FakeWS(), _FakeWS()
    p1 = _FakePrincipal(subject_id=id1)
    p2 = _FakePrincipal(subject_id=id2)

    await hub.deckmux_on_browser_connect("w1", ws1, "viewer", principal=p1)
    result = await hub.deckmux_on_browser_connect("w1", ws2, "viewer", principal=p2)

    assert result is not None
    colors = [u["color"] for u in result["users"]]
    assert colors[0] != colors[1]  # taken_colors prevented the collision


@pytest.mark.asyncio
async def test_second_connect_broadcasts_sync_to_existing_users() -> None:
    """When user2 joins, existing users receive presence_sync so they see user2's avatar."""
    hub = _FakeHub()
    ws1, ws2 = _FakeWS(), _FakeWS()

    # First user connects — no broadcast needed (they're the only one)
    await hub.deckmux_on_browser_connect("w1", ws1, "viewer")
    hub.broadcast.assert_not_called()

    # Second user connects — broadcast must fire so ws1 sees ws2
    await hub.deckmux_on_browser_connect("w1", ws2, "operator")
    hub.broadcast.assert_called_once()
    worker_id_arg, msg = hub.broadcast.call_args[0]
    assert worker_id_arg == "w1"
    assert msg["type"] == "presence_sync"
    assert len(msg["users"]) == 2  # both users included


@pytest.mark.asyncio
async def test_first_connect_does_not_broadcast() -> None:
    """First user connecting should not trigger a broadcast (no one to notify)."""
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "viewer")
    hub.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_authenticated_disconnect_uses_subject_id() -> None:
    """Disconnect with principal uses subject_id, not str(id(ws)), so the store lookup succeeds."""
    hub = _FakeHub()
    ws = _FakeWS()
    principal = _FakePrincipal(subject_id="alice")
    await hub.deckmux_on_browser_connect("w1", ws, "operator", principal=principal)
    hub.broadcast.reset_mock()

    # Disconnect passing the same principal — must find and remove the user
    await hub.deckmux_on_browser_disconnect("w1", ws, principal=principal)

    hub.broadcast.assert_called_once()
    msg = hub.broadcast.call_args[0][1]
    assert msg["type"] == "presence_leave"
    assert msg["user_id"] == "alice"


@pytest.mark.asyncio
async def test_authenticated_disconnect_wrong_ws_id_ghost_absence() -> None:
    """Without principal, disconnect with a different ws object leaves a ghost user."""
    hub = _FakeHub()
    ws_connect = _FakeWS()
    ws_disconnect = _FakeWS()  # different object → different str(id(...))
    principal = _FakePrincipal(subject_id="bob")
    await hub.deckmux_on_browser_connect("w1", ws_connect, "operator", principal=principal)
    hub.broadcast.reset_mock()

    # Disconnect without principal — str(id(ws_disconnect)) != "bob" → ghost remains
    await hub.deckmux_on_browser_disconnect("w1", ws_disconnect)
    hub.broadcast.assert_not_called()  # no leave broadcast; user is still in store
