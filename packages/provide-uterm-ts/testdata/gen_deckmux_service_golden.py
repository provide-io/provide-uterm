#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the DeckMux presence service.

This is the part several browsers talk to at once, so what it broadcasts — and
what it declines to broadcast — is what everybody else sees.

**An anonymous identity is minted, not derived from the object.** The
reference is explicit that ``id(ws)`` is unsafe: CPython reuses an address
after collection, so a browser connecting now can be handed the presence and
the ownership of one that disconnected a moment ago. A ``uuid4`` stashed on
the connection avoids that. The corpus drives a deterministic uuid source so
the sequence is reproducible while keeping the identities distinct.

**A malformed presence update is dropped, not fatal.** The oversized
``selection`` or ``pin`` a browser can send raises inside the store; the
service swallows it, mutates nothing and broadcasts nothing, so a session
survives being poked at rather than tearing down.

**Control requests are a three-way decision.** Nobody holding control grants
it; the holder asking again releases it; anybody else asking is ignored. The
last is the one that matters — without it any viewer could take the terminal
from whoever is typing.

**The first browser is told, the rest are told about each other.** A sync goes
back to the joiner always, and out to everybody only once there is somebody
else to tell.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_deckmux_service_golden.py
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from provide.uterm.deckmux import _names as names_module
from provide.uterm.deckmux._service import DeckMuxPresence

OUT = Path(__file__).with_name("deckmux_service_golden.json")


class _Hub:
    """A hub that records what it was asked to broadcast."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def broadcast(self, worker_id: str, msg: dict[str, Any]) -> None:
        self.sent.append({"worker_id": worker_id, "msg": msg})


class _Socket:
    """A browser connection the service can stash an identity on."""


class _Principal:
    """An authenticated principal, as the hub hands them over."""

    def __init__(self, subject_id: str, display_name: str | None = None) -> None:
        self.subject_id = subject_id
        if display_name is not None:
            self.display_name = display_name


class _Hex:
    """Just enough of a UUID for the code under test."""

    def __init__(self, value: str) -> None:
        self.hex = value


_COUNTER = 0


def _reset_uuids() -> None:
    """Start the identity counter again.

    Per scenario rather than per run, so each one replays on its own — a
    single global counter would make every scenario's identities depend on
    how many connections the ones before it happened to make.
    """
    global _COUNTER
    _COUNTER = 0


def _deterministic_uuids() -> None:
    """Replace uuid4 with a counter so the corpus is reproducible."""

    def _next() -> Any:
        global _COUNTER
        _COUNTER += 1
        return _Hex(f"{_COUNTER:032x}")

    uuid.uuid4 = _next  # type: ignore[assignment]


def _wire(hub: _Hub) -> list[dict[str, Any]]:
    """The broadcasts recorded so far, then forgotten."""
    sent = list(hub.sent)
    hub.sent.clear()
    return sent


async def _scenario_joining() -> dict[str, Any]:
    """Who is told what when browsers arrive and leave."""
    hub = _Hub()
    service = DeckMuxPresence(hub)
    first, second = _Socket(), _Socket()

    first_sync = await service.on_browser_connect("w1", first, "operator")
    after_first = _wire(hub)
    second_sync = await service.on_browser_connect("w1", second, "viewer")
    after_second = _wire(hub)

    await service.on_browser_disconnect("w1", second)
    after_leave = _wire(hub)
    # Leaving twice: the second has nobody to remove and so tells nobody.
    await service.on_browser_disconnect("w1", second)
    after_second_leave = _wire(hub)

    return {
        "first_sync": first_sync,
        "broadcast_after_first": after_first,
        "second_sync": second_sync,
        "broadcast_after_second": after_second,
        "broadcast_after_leave": after_leave,
        "broadcast_after_leaving_twice": after_second_leave,
    }


async def _scenario_identity() -> dict[str, Any]:
    """How a participant is named."""
    hub = _Hub()
    service = DeckMuxPresence(hub)

    anonymous = await service.on_browser_connect("w1", _Socket(), "viewer")
    named = await service.on_browser_connect("w2", _Socket(), "viewer", _Principal("sre:alice", "Alice"))
    unnamed = await service.on_browser_connect("w3", _Socket(), "viewer", _Principal("sre:bob"))
    blank = await service.on_browser_connect("w4", _Socket(), "viewer", _Principal("sre:carol", ""))
    _wire(hub)

    return {
        "anonymous": anonymous,
        "named": named,
        "unnamed_principal": unnamed,
        "blank_display_name": blank,
    }


async def _scenario_stable_identity() -> dict[str, Any]:
    """One connection object keeps its identity across calls.

    An identity that changed between connect and update would leave the update
    unable to find the participant the connect had added.
    """
    service = DeckMuxPresence(_Hub())
    stable = _Socket()
    first = await service.on_browser_connect("w1", stable, "viewer")
    await service.on_browser_disconnect("w1", stable)
    again = await service.on_browser_connect("w1", stable, "viewer")
    return {"first": first, "again": again}


async def _scenario_updates() -> dict[str, Any]:
    """What a presence update does, and what a bad one does not."""
    hub = _Hub()
    service = DeckMuxPresence(hub)
    ws = _Socket()
    await service.on_browser_connect("w1", ws, "operator")
    _wire(hub)

    await service.handle_message("w1", ws, {"type": "presence_update", "scroll_line": 5, "typing": True})
    good = _wire(hub)

    await service.handle_message("w1", ws, {"type": "presence_update", "scroll_line": 99, "pin": {"a": "x" * 4000}})
    oversized = _wire(hub)

    await service.handle_message("w1", ws, {"type": "presence_update", "nonsense": 1})
    unknown_field = _wire(hub)

    await service.handle_message("w1", _Socket(), {"type": "presence_update", "scroll_line": 1})
    stranger = _wire(hub)

    await service.handle_message("w1", ws, {"type": "nonsense"})
    unknown_type = _wire(hub)

    store = service.get_presence_store("w1")
    user = store.get(next(iter(p.user_id for p in store.get_all())))

    return {
        "broadcast_on_update": good,
        "broadcast_on_oversized_pin": oversized,
        "state_after_oversized_pin": user.to_dict() if user else None,
        "broadcast_on_unknown_field": unknown_field,
        "broadcast_from_a_stranger": stranger,
        "broadcast_on_unknown_type": unknown_type,
    }


async def _scenario_queue() -> dict[str, Any]:
    """Keystrokes from somebody who cannot type yet."""
    hub = _Hub()
    service = DeckMuxPresence(hub)
    ws = _Socket()
    await service.on_browser_connect("w1", ws, "viewer")
    _wire(hub)

    await service.handle_message("w1", ws, {"type": "queued_input", "keys": "ls"})
    queued = _wire(hub)
    await service.handle_message("w1", ws, {"type": "queued_input", "keys": "\x1b[A"})
    queued_arrow = _wire(hub)
    await service.handle_message("w1", ws, {"type": "queued_input"})
    queued_nothing = _wire(hub)
    await service.handle_message("w1", _Socket(), {"type": "queued_input", "keys": "rm"})
    queued_stranger = _wire(hub)

    return {
        "broadcast_on_queue": queued,
        "broadcast_on_arrow": queued_arrow,
        "broadcast_on_no_keys": queued_nothing,
        "broadcast_from_a_stranger": queued_stranger,
    }


async def _scenario_control() -> dict[str, Any]:
    """Who gets the terminal."""
    hub = _Hub()
    service = DeckMuxPresence(hub)
    first, second = _Socket(), _Socket()
    await service.on_browser_connect("w1", first, "operator")
    await service.on_browser_connect("w1", second, "viewer")
    _wire(hub)

    await service.handle_message("w1", first, {"type": "control_request"})
    granted = _wire(hub)
    owner_after_grant = service.get_presence_store("w1").get_owner()

    # Somebody else asking while it is held is ignored — otherwise any viewer
    # could take the terminal from whoever is typing.
    await service.handle_message("w1", second, {"type": "control_request"})
    refused = _wire(hub)
    owner_after_refusal = service.get_presence_store("w1").get_owner()

    await service.handle_message("w1", first, {"type": "control_request"})
    released = _wire(hub)
    owner_after_release = service.get_presence_store("w1").get_owner()

    # With control free, the one who was refused can now have it, and their
    # queued keystrokes travel with it.
    await service.handle_message("w1", second, {"type": "queued_input", "keys": "ls"})
    _wire(hub)
    await service.handle_message("w1", second, {"type": "control_request"})
    granted_with_queue = _wire(hub)

    return {
        "broadcast_on_grant": granted,
        "owner_after_grant": owner_after_grant.user_id if owner_after_grant else None,
        "broadcast_on_refusal": refused,
        "owner_after_refusal": owner_after_refusal.user_id if owner_after_refusal else None,
        "broadcast_on_release": released,
        "owner_after_release": owner_after_release.user_id if owner_after_release else None,
        "broadcast_on_grant_with_a_queue": granted_with_queue,
    }


async def _scenario_guards() -> dict[str, Any]:
    """What a browser is not allowed to say about itself.

    The field set is an allow-list, so a browser cannot promote itself to
    owner, hand itself a colour, or write its own queue display — each of
    which is a claim only the server gets to make.
    """
    hub = _Hub()
    service = DeckMuxPresence(hub)
    first, second = _Socket(), _Socket()
    await service.on_browser_connect("w1", first, "operator")
    await service.on_browser_connect("w1", second, "viewer")
    _wire(hub)

    await service.handle_message("w1", second, {"type": "presence_update", "is_owner": True})
    claimed_ownership = _wire(hub)
    owner_after_claim = service.get_presence_store("w1").get_owner()

    await service.handle_message("w1", second, {"type": "presence_update", "color": "#000000", "name": "Administrator"})
    claimed_identity = _wire(hub)

    await service.handle_message("w1", second, {"type": "presence_update", "queued_keys": "rm -rf /"})
    claimed_queue = _wire(hub)

    selection = {"start": {"row": 1, "col": 2}, "end": {"row": 3, "col": 4}}
    await service.handle_message("w1", second, {"type": "presence_update", "selection": selection})
    sent_selection = _wire(hub)

    return {
        "broadcast_on_claimed_ownership": claimed_ownership,
        "owner_after_claim": owner_after_claim.user_id if owner_after_claim else None,
        "broadcast_on_claimed_identity": claimed_identity,
        "broadcast_on_claimed_queue": claimed_queue,
        "broadcast_on_selection": sent_selection,
    }


async def _scenario_colors() -> dict[str, Any]:
    """Every participant in a session is a different colour.

    Two people rendered identically cannot be told apart in a shared
    terminal, so the walk has to see what the store already holds.
    """
    hub = _Hub()
    service = DeckMuxPresence(hub)
    # As many browsers as there are colours, so the walk has to run: with a
    # smaller room the natural picks may happen not to collide at all.
    for _ in range(len(names_module._COLORS)):
        await service.on_browser_connect("w1", _Socket(), "viewer")
    _wire(hub)
    users = service.get_presence_store("w1").get_all()
    return {
        "colors": [user.color for user in users],
        "all_distinct": len({user.color for user in users}) == len(users),
    }


async def _scenario_prune() -> dict[str, Any]:
    """A browser that dropped without saying so is cleared by the next joiner.

    Otherwise it holds a colour and a slot in everybody's participant list
    forever, as a cursor that never moves.
    """
    hub = _Hub()
    service = DeckMuxPresence(hub)
    stale = _Socket()
    await service.on_browser_connect("w1", stale, "viewer")
    _wire(hub)

    store = service.get_presence_store("w1")
    ghost = store.get_all()[0]
    ghost_id = ghost.user_id
    # Older than the debris window, as a connection that died without a close
    # frame would look.
    ghost.last_activity_at = time.time() - 100

    joiner_sync = await service.on_browser_connect("w1", _Socket(), "viewer")
    _wire(hub)

    return {
        "ghost_id": ghost_id,
        "users_after_join": [user["user_id"] for user in joiner_sync["users"]],
        "ghost_survived": any(user["user_id"] == ghost_id for user in joiner_sync["users"]),
    }


async def _scenario_containers() -> dict[str, Any]:
    """Per-worker containers, and what cleanup takes."""
    hub = _Hub()
    service = DeckMuxPresence(hub)

    store_a = service.get_presence_store("w1")
    store_b = service.get_presence_store("w2")
    same_store = service.get_presence_store("w1") is store_a

    default_tm = service.get_transfer_manager("w1")
    configured_tm = service.get_transfer_manager("w2", {"auto_transfer_idle_s": 5, "keystroke_queue": "replay"})
    # Config only applies at creation; asking again with different settings
    # returns the manager that already exists.
    ignored_config = service.get_transfer_manager("w2", {"auto_transfer_idle_s": 999})

    service.cleanup("w1")
    after_cleanup_is_new = service.get_presence_store("w1") is not store_a
    service.cleanup("nobody")

    return {
        "separate_stores": store_a is not store_b,
        "same_store_when_asked_twice": same_store,
        "default_idle_s": default_tm._auto_idle_s,
        "default_queue_mode": default_tm.queue_mode,
        "configured_idle_s": configured_tm._auto_idle_s,
        "configured_queue_mode": configured_tm.queue_mode,
        "config_ignored_on_second_call": ignored_config._auto_idle_s,
        "cleanup_replaces_the_store": after_cleanup_is_new,
    }


async def _build() -> dict[str, Any]:
    """Run every scenario in order under the deterministic uuid source."""
    scenarios = {
        "joining": _scenario_joining,
        "identity": _scenario_identity,
        "stable_identity": _scenario_stable_identity,
        "updates": _scenario_updates,
        "queue": _scenario_queue,
        "control": _scenario_control,
        "guards": _scenario_guards,
        "colors": _scenario_colors,
        "prune": _scenario_prune,
        "containers": _scenario_containers,
    }
    recorded: dict[str, Any] = {}
    for name, scenario in scenarios.items():
        _reset_uuids()
        recorded[name] = await scenario()
    return recorded


def main() -> int:
    """Write the golden corpus and report the scenario count."""
    _deterministic_uuids()
    corpus = asyncio.run(_build())
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus)} scenarios)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
