#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Surgical mutmut kills for ``deckmux/_service.py``.

Targets the genuinely-killable survivors surfaced when ``_service.py``
entered the mutation perimeter: the ``"display_name"`` lookup key in
``on_browser_connect`` and the ``"total_lines"`` field key in the
``handle_message`` presence-update copy list. (The remaining survivors on
those functions — the ``getattr`` defaults that feed an ``or`` fallback and
the ``principal and ...`` truthiness flip — are equivalent mutants: see
``docs/mutmut-survivors-triage.md``.)
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from provide.uterm.deckmux._hub_mixin import DeckMuxMixin
from provide.uterm.deckmux._protocol import MSG_PRESENCE_UPDATE

pytestmark = pytest.mark.asyncio


class _FakeHub(DeckMuxMixin):
    def __init__(self) -> None:
        self._deckmux_init()
        self.broadcast = AsyncMock()


@dataclass
class _FakePrincipal:
    subject_id: str
    display_name: str = ""


@dataclass
class _SubjectOnlyPrincipal:
    """Truthy principal that has ``subject_id`` but no ``display_name`` attr."""

    subject_id: str


class _NoSubjectPrincipal:
    """Truthy principal object with no ``subject_id`` attribute at all.

    Used to kill the ``principal and hasattr(...)`` -> ``principal or
    hasattr(...)`` mutants: with ``or``, a truthy principal short-circuits
    into the branch and dereferences the missing ``subject_id`` (AttributeError).
    """


class _FakeWS:
    """Distinct per-connection ws object."""


async def test_connect_uses_display_name_as_presence_name() -> None:
    """``getattr(principal, "display_name", ...)`` must read the real key.

    Kills the ``"display_name" -> "XXdisplay_nameXX"`` mutant: with the key
    mutated, the lookup misses and the name falls back to the subject id.
    """
    hub = _FakeHub()
    principal = _FakePrincipal(subject_id="sre:alice", display_name="Alice")

    await hub.deckmux_on_browser_connect("w1", _FakeWS(), "operator", principal=principal)

    user = hub._get_presence_store("w1").get("sre:alice")
    assert user is not None
    assert user.name == "Alice"  # not "sre:alice"


async def test_connect_without_display_name_falls_back_to_subject() -> None:
    """A principal with ``subject_id`` but no ``display_name`` uses the subject.

    Exercises the ``hasattr(principal, "display_name")`` False branch (kills the
    ``and`` -> ``or`` and if/else mutants on the display-name lookup).
    """
    hub = _FakeHub()
    await hub.deckmux_on_browser_connect("w1", _FakeWS(), "operator", principal=_SubjectOnlyPrincipal("sre:bob"))

    user = hub._get_presence_store("w1").get("sre:bob")
    assert user is not None
    assert user.name == "sre:bob"


async def test_connect_with_empty_display_name_falls_back_to_subject() -> None:
    """An empty ``display_name`` must fall back to the subject id (truthiness branch)."""
    hub = _FakeHub()
    await hub.deckmux_on_browser_connect(
        "w1", _FakeWS(), "operator", principal=_FakePrincipal(subject_id="sre:carol", display_name="")
    )

    user = hub._get_presence_store("w1").get("sre:carol")
    assert user is not None
    assert user.name == "sre:carol"


async def test_connect_principal_without_subject_id_uses_anonymous() -> None:
    """``principal and hasattr(...)`` must be ``and`` not ``or``.

    A truthy principal lacking ``subject_id`` must take the anonymous path. The
    ``or`` mutant would instead enter the branch and raise AttributeError.
    """
    hub = _FakeHub()
    result = await hub.deckmux_on_browser_connect("w1", _FakeWS(), "viewer", principal=_NoSubjectPrincipal())
    assert result is not None  # completed via the generate_name() path, no AttributeError


async def test_presence_update_propagates_total_lines() -> None:
    """``"total_lines"`` must be in the copied presence-update field set.

    Kills the ``"total_lines" -> "XXtotal_linesXX"`` / ``"TOTAL_LINES"``
    mutants: with the key mutated the field is dropped from ``fields`` and
    never reaches the broadcast payload (staying at the default 0).
    """
    hub = _FakeHub()
    ws = _FakeWS()
    principal = _FakePrincipal(subject_id="sre:alice", display_name="Alice")
    await hub.deckmux_on_browser_connect("w1", ws, "operator", principal=principal)
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message(
        "w1",
        ws,
        {"type": MSG_PRESENCE_UPDATE, "total_lines": 42},
        principal=principal,
    )

    # The update must have broadcast with the real total_lines value.
    assert hub.broadcast.await_count == 1
    _worker_id, payload = hub.broadcast.await_args.args
    assert payload["type"] == MSG_PRESENCE_UPDATE
    assert payload["total_lines"] == 42

    # And it must be reflected on the stored user.
    user = hub._get_presence_store("w1").get("sre:alice")
    assert user is not None
    assert user.total_lines == 42


async def test_disconnect_principal_without_subject_id_is_safe() -> None:
    """``principal and hasattr(...)`` in disconnect must be ``and`` not ``or``.

    The ``or`` mutant short-circuits a truthy principal into the branch and
    dereferences the missing ``subject_id`` (AttributeError).
    """
    hub = _FakeHub()
    # Must not raise (correct ``and`` takes the anonymous path).
    await hub.deckmux_on_browser_disconnect("w1", _FakeWS(), principal=_NoSubjectPrincipal())


async def test_handle_message_principal_without_subject_id_is_safe() -> None:
    """``principal and hasattr(...)`` in handle_message must be ``and`` not ``or``."""
    hub = _FakeHub()
    # Must not raise; an unknown anonymous user simply produces no broadcast.
    await hub.deckmux_handle_message(
        "w1",
        _FakeWS(),
        {"type": MSG_PRESENCE_UPDATE, "total_lines": 7},
        principal=_NoSubjectPrincipal(),
    )


async def test_presence_update_with_valid_selection_broadcasts() -> None:
    """A small, well-formed ``selection`` is stored and broadcast."""
    hub = _FakeHub()
    ws = _FakeWS()
    principal = _FakePrincipal(subject_id="sre:alice", display_name="Alice")
    await hub.deckmux_on_browser_connect("w1", ws, "operator", principal=principal)
    hub.broadcast.reset_mock()

    sel = {"start": {"row": 1, "col": 2}, "end": {"row": 3, "col": 4}}
    await hub.deckmux_handle_message(
        "w1",
        ws,
        {"type": MSG_PRESENCE_UPDATE, "selection": sel},
        principal=principal,
    )

    assert hub.broadcast.await_count == 1
    _worker_id, payload = hub.broadcast.await_args.args
    assert payload["selection"] == sel
    user = hub._get_presence_store("w1").get("sre:alice")
    assert user is not None
    assert user.selection == sel


async def test_presence_update_with_oversized_selection_is_dropped() -> None:
    """An oversized ``selection`` must NOT raise out of the handler and must NOT broadcast.

    The new size validation rejects the value; the service treats the rejected
    update as a no-op (no store mutation, no broadcast) rather than tearing down
    the session.
    """
    hub = _FakeHub()
    ws = _FakeWS()
    principal = _FakePrincipal(subject_id="sre:alice", display_name="Alice")
    await hub.deckmux_on_browser_connect("w1", ws, "operator", principal=principal)
    hub.broadcast.reset_mock()

    big = {"blob": "x" * 4096}  # json well over the 2KB cap
    # Must not raise.
    await hub.deckmux_handle_message(
        "w1",
        ws,
        {"type": MSG_PRESENCE_UPDATE, "selection": big},
        principal=principal,
    )

    # No broadcast and no store mutation.
    assert hub.broadcast.await_count == 0
    user = hub._get_presence_store("w1").get("sre:alice")
    assert user is not None
    assert user.selection is None
