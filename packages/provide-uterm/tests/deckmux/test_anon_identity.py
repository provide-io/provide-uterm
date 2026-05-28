#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Anonymous DeckMux identity must be a stable per-connection UUID.

``str(id(ws))`` is unsafe because CPython reuses an object's ``id()`` after
garbage collection: a freshly-connected browser could collide with a
disconnected one's id and inherit its presence/ownership. The service mints
a per-connection ``uuid4`` instead.
"""

from __future__ import annotations

from provide.uterm.deckmux._service import _ANON_ID_ATTR, _anon_user_id


class _FakeWS:
    """Plain object that allows attribute assignment (no __slots__)."""


class _SlottedWS:
    """A ws object that forbids arbitrary attribute assignment."""

    __slots__ = ()


def test_anon_id_is_not_object_id() -> None:
    ws = _FakeWS()
    assert _anon_user_id(ws) != str(id(ws))


def test_anon_id_is_stable_per_connection() -> None:
    ws = _FakeWS()
    first = _anon_user_id(ws)
    second = _anon_user_id(ws)
    assert first == second
    # The id is stashed on the connection object.
    assert getattr(ws, _ANON_ID_ATTR) == first


def test_distinct_connections_get_distinct_ids() -> None:
    ws_a = _FakeWS()
    ws_b = _FakeWS()
    assert _anon_user_id(ws_a) != _anon_user_id(ws_b)


def test_id_reuse_does_not_alias_identity() -> None:
    # Simulate CPython id() reuse: a new object can share a previously-seen
    # id once the old one is collected. The minted identity must NOT collide.
    ws_a = _FakeWS()
    id_a = _anon_user_id(ws_a)
    # A pre-existing stale token (what id-reuse would have produced) must not
    # match a freshly minted one.
    ws_b = _FakeWS()
    assert _anon_user_id(ws_b) != id_a


def test_anon_id_unique_even_when_stash_unavailable() -> None:
    # Objects that forbid attribute assignment still get a unique id per call;
    # uniqueness (the security property) is preserved even if stability is not.
    ws = _SlottedWS()
    first = _anon_user_id(ws)
    second = _anon_user_id(ws)
    assert first != str(id(ws))
    assert second != str(id(ws))
    # Two separate mints (no stash) — must still be distinct UUIDs.
    assert first != second
