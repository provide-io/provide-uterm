#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.deckmux._presence — per-session user tracking."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from provide.terminal.deckmux._presence import PresenceStore, UserPresence

# --- UserPresence ---


def test_user_presence_defaults() -> None:
    p = UserPresence(user_id="u1", name="Alice", color="#fff", role="admin")
    assert p.scroll_line == 0
    assert p.scroll_range == (0, 0)
    assert p.selection is None
    assert p.pin is None
    assert p.typing is False
    assert p.queued_keys == ""
    assert p.is_owner is False
    assert p.initials == ""


def test_user_presence_to_dict() -> None:
    p = UserPresence(user_id="u1", name="Alice", color="#fff", role="admin", initials="AL")
    d = p.to_dict()
    assert d["user_id"] == "u1"
    assert d["name"] == "Alice"
    assert d["color"] == "#fff"
    assert d["role"] == "admin"
    assert d["initials"] == "AL"
    assert d["scroll_range"] == [0, 0]  # tuple -> list
    assert d["is_owner"] is False


def test_user_presence_to_dict_with_selection_and_pin() -> None:
    p = UserPresence(
        user_id="u1",
        name="A",
        color="#000",
        role="viewer",
        selection={"start": 1, "end": 5},
        pin={"line": 10},
    )
    d = p.to_dict()
    assert d["selection"] == {"start": 1, "end": 5}
    assert d["pin"] == {"line": 10}


def test_user_presence_is_idle_true() -> None:
    p = UserPresence(user_id="u1", name="A", color="#000", role="viewer")
    p.last_activity_at = time.time() - 60
    assert p.is_idle(30.0) is True


def test_user_presence_is_idle_false() -> None:
    p = UserPresence(user_id="u1", name="A", color="#000", role="viewer")
    p.last_activity_at = time.time()
    assert p.is_idle(30.0) is False


# --- PresenceStore ---


def test_store_add_and_get() -> None:
    store = PresenceStore()
    p = store.add("u1", "Alice", "#fff", "admin", initials="AL")
    assert p.user_id == "u1"
    assert store.get("u1") is p
    assert store.count == 1


def test_store_get_missing() -> None:
    store = PresenceStore()
    assert store.get("nonexistent") is None


def test_store_update() -> None:
    store = PresenceStore()
    store.add("u1", "Alice", "#fff", "admin")
    with patch("provide.terminal.deckmux._presence.time") as mock_time:
        mock_time.time.return_value = 999.0
        p = store.update("u1", typing=True, scroll_line=42)
    assert p is not None
    assert p.typing is True
    assert p.scroll_line == 42
    assert p.last_activity_at == 999.0


def test_store_update_missing() -> None:
    store = PresenceStore()
    assert store.update("nonexistent", typing=True) is None


def test_store_update_rejects_nonexistent_attrs() -> None:
    store = PresenceStore()
    store.add("u1", "Alice", "#fff", "admin")
    with pytest.raises(ValueError, match="nonexistent_field"):
        store.update("u1", nonexistent_field="value")


def test_store_remove() -> None:
    store = PresenceStore()
    store.add("u1", "Alice", "#fff", "admin")
    removed = store.remove("u1")
    assert removed is not None
    assert removed.user_id == "u1"
    assert store.count == 0
    assert store.get("u1") is None


def test_store_remove_missing() -> None:
    store = PresenceStore()
    assert store.remove("nonexistent") is None


def test_store_get_all() -> None:
    store = PresenceStore()
    store.add("u1", "Alice", "#fff", "admin")
    store.add("u2", "Bob", "#000", "viewer")
    all_users = store.get_all()
    assert len(all_users) == 2
    ids = {p.user_id for p in all_users}
    assert ids == {"u1", "u2"}


def test_store_get_all_empty() -> None:
    store = PresenceStore()
    assert store.get_all() == []


# --- Owner tracking ---


def test_store_set_owner() -> None:
    store = PresenceStore()
    store.add("u1", "Alice", "#fff", "admin")
    store.add("u2", "Bob", "#000", "viewer")
    store.set_owner("u1")
    assert store.get("u1").is_owner is True  # type: ignore[union-attr]
    assert store.get("u2").is_owner is False  # type: ignore[union-attr]


def test_store_set_owner_clears_previous() -> None:
    store = PresenceStore()
    store.add("u1", "Alice", "#fff", "admin")
    store.add("u2", "Bob", "#000", "viewer")
    store.set_owner("u1")
    store.set_owner("u2")
    assert store.get("u1").is_owner is False  # type: ignore[union-attr]
    assert store.get("u2").is_owner is True  # type: ignore[union-attr]


def test_store_get_owner() -> None:
    store = PresenceStore()
    store.add("u1", "Alice", "#fff", "admin")
    assert store.get_owner() is None
    store.set_owner("u1")
    owner = store.get_owner()
    assert owner is not None
    assert owner.user_id == "u1"


def test_store_get_owner_none() -> None:
    store = PresenceStore()
    store.add("u1", "Alice", "#fff", "admin")
    assert store.get_owner() is None


def test_store_clear_owner() -> None:
    store = PresenceStore()
    store.add("u1", "Alice", "#fff", "admin")
    store.set_owner("u1")
    store.clear_owner()
    assert store.get("u1").is_owner is False  # type: ignore[union-attr]
    assert store.get_owner() is None


# --- Sync payload ---


def test_store_get_sync_payload() -> None:
    store = PresenceStore()
    store.add("u1", "Alice", "#fff", "admin")
    config = {"idle_timeout": 30}
    payload = store.get_sync_payload(config)
    assert payload["type"] == "presence_sync"
    assert len(payload["users"]) == 1
    assert payload["users"][0]["user_id"] == "u1"
    assert payload["config"] == config


def test_store_get_sync_payload_empty() -> None:
    store = PresenceStore()
    payload = store.get_sync_payload({})
    assert payload["users"] == []


# --- Taken colors ---


def test_store_taken_colors() -> None:
    store = PresenceStore()
    store.add("u1", "Alice", "#e74c3c", "admin")
    store.add("u2", "Bob", "#3498db", "viewer")
    taken = store.taken_colors()
    assert taken == frozenset({"#e74c3c", "#3498db"})


def test_store_taken_colors_empty() -> None:
    store = PresenceStore()
    assert store.taken_colors() == frozenset()


# --- Count ---


def test_store_count() -> None:
    store = PresenceStore()
    assert store.count == 0
    store.add("u1", "Alice", "#fff", "admin")
    assert store.count == 1
    store.add("u2", "Bob", "#000", "viewer")
    assert store.count == 2
    store.remove("u1")
    assert store.count == 1


def test_store_prune_idle_removes_stale() -> None:
    store = PresenceStore()
    store.add("u1", "Alice", "#fff", "admin")
    store.add("u2", "Bob", "#000", "viewer")
    store.add("u3", "Carol", "#aaa", "operator")
    # Backdate u3's activity to make it appear idle
    store._users["u3"].last_activity_at = time.time() - 120
    pruned = store.prune_idle(60.0)
    assert pruned == ["u3"]
    assert store.count == 2
    assert store.get("u3") is None


def test_store_prune_idle_no_stale() -> None:
    store = PresenceStore()
    store.add("u1", "Alice", "#fff", "admin")
    pruned = store.prune_idle(60.0)
    assert pruned == []
    assert store.count == 1


def test_add_default_initials_is_empty_string_not_xxxx() -> None:
    """Default initials is '', not 'XXXX' or None."""
    store = PresenceStore()
    store.add("u1", "Alice", "#fff", "admin")
    user = store.get("u1")
    assert user is not None
    assert user.initials == ""
    assert user.initials is not None
