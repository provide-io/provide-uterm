#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript hub-models port.

Two things here are worth recording rather than remembering.

The first is the lease boundary. ``is_*_active`` asks ``expires_at > now``
while ``expire`` asks ``expires_at <= now``, so a lease expiring at exactly
``now`` is simultaneously inactive *and* expired — deliberate, and easy to
get backwards in a reimplementation. The predicate table below drives both
sides of that boundary from the reference.

The second is the event deque. ``WorkerTermState.events`` is a
``deque(maxlen=2000)``: appending past the cap silently drops from the
*front*, and rebuilding it with a smaller maxlen — which the hub does on
worker connect — truncates immediately, keeping the newest. A plain array
does neither.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_hub_models_golden.py
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

from provide.uterm.bridge.coordinator import HijackSession
from provide.uterm.server.bridge.models import VALID_ROLES, HijackLease, WorkerTermState

OUT = Path(__file__).with_name("hub_models_golden.json")

NOW = 1000.0

# (name, ws_present, ws_expires_at, session_expires_at)
LEASE_CASES: list[tuple[str, bool, float | None, float | None]] = [
    ("idle", False, None, None),
    ("dashboard, unexpired", True, NOW + 10.0, None),
    ("dashboard, expiring exactly now", True, NOW, None),
    ("dashboard, expired", True, NOW - 10.0, None),
    ("dashboard slot with no expiry", True, None, None),
    ("rest, unexpired", False, None, NOW + 10.0),
    ("rest, expiring exactly now", False, None, NOW),
    ("rest, expired", False, None, NOW - 10.0),
    ("both unexpired", True, NOW + 10.0, NOW + 10.0),
    ("dashboard expired, rest live", True, NOW - 10.0, NOW + 10.0),
    ("dashboard live, rest expired", True, NOW + 10.0, NOW - 10.0),
    ("both expired", True, NOW - 10.0, NOW - 10.0),
]


def _lease(ws_present: bool, ws_expires_at: float | None, session_expires_at: float | None) -> HijackLease:
    """Build a lease from the three slots the cases vary."""
    session = (
        None
        if session_expires_at is None
        else HijackSession(hijack_id="h1", owner="operator", lease_expires_at=session_expires_at)
    )
    return HijackLease(ws="ws" if ws_present else None, ws_expires_at=ws_expires_at, session=session)


def _lease_record() -> list[dict[str, Any]]:
    """Predicates and the effect of expire(), for every slot combination."""
    records = []
    for name, ws_present, ws_expires_at, session_expires_at in LEASE_CASES:
        lease = _lease(ws_present, ws_expires_at, session_expires_at)
        before = {
            "is_idle": lease.is_idle,
            "is_dashboard_active": lease.is_dashboard_active(NOW),
            "is_rest_active": lease.is_rest_active(NOW),
            "is_active": lease.is_active(NOW),
        }
        # expire() mutates, so it runs on a second lease built the same way.
        victim = _lease(ws_present, ws_expires_at, session_expires_at)
        rest_expired, dash_expired = victim.expire(NOW)
        records.append(
            {
                "name": name,
                "ws_present": ws_present,
                "ws_expires_at": ws_expires_at,
                "session_expires_at": session_expires_at,
                **before,
                "rest_expired": rest_expired,
                "dash_expired": dash_expired,
                "ws_after": victim.ws,
                "ws_expires_at_after": victim.ws_expires_at,
                "session_after": None if victim.session is None else victim.session.lease_expires_at,
                "is_idle_after": victim.is_idle,
            }
        )
    return records


def _activity_record() -> dict[str, Any]:
    """``last_activity_at`` is seeded from the monotonic clock, not from zero.

    A state created and never touched must not look infinitely idle to the
    pruner, so the default is *now* rather than the falsy zero a plain struct
    default would give. Only the shape of that is recordable, not a value.
    """
    first = WorkerTermState()
    second = WorkerTermState()
    return {
        "defaults_to_zero": first.last_activity_at == 0.0,
        "is_monotonic_now": first.last_activity_at > 0.0,
        "does_not_go_backwards": second.last_activity_at >= first.last_activity_at,
    }


def _state_record() -> dict[str, Any]:
    """Dataclass defaults, and the lease view's copy semantics."""
    state = WorkerTermState()
    defaults = {
        "worker_ws_is_none": state.worker_ws is None,
        "browsers": dict(state.browsers),
        "hijack_owner_is_none": state.hijack_owner is None,
        "hijack_owner_expires_at": state.hijack_owner_expires_at,
        "hijack_session_is_none": state.hijack_session is None,
        "hijack_pending": state.hijack_pending,
        "input_mode": state.input_mode,
        "last_snapshot_is_none": state.last_snapshot is None,
        "events": list(state.events),
        "events_maxlen": state.events.maxlen,
        "event_seq": state.event_seq,
        "min_event_seq": state.min_event_seq,
        "protocol_version": state.protocol_version,
        "is_tunnel_worker": state.is_tunnel_worker,
        "graphical_session_is_none": state.graphical_session is None,
    }

    # The lease view is a fresh object each call: mutating it must not reach
    # back into the state, and two reads must not be the same object.
    state.hijack_owner = "ws"
    state.hijack_owner_expires_at = NOW + 10.0
    view = state.lease
    view.ws = None
    view.ws_expires_at = None
    leaked = state.hijack_owner is None
    same_object = state.lease is state.lease

    # apply_lease is how a mutated view is written back.
    written = WorkerTermState()
    written.apply_lease(HijackLease(ws="ws2", ws_expires_at=NOW + 20.0, session=None))

    return {
        "defaults": defaults,
        "view_mutation_leaked": leaked,
        "view_is_same_object": same_object,
        "applied_ws": written.hijack_owner,
        "applied_expires_at": written.hijack_owner_expires_at,
        "applied_session_is_none": written.hijack_session is None,
        "valid_roles": sorted(VALID_ROLES),
        "activity": _activity_record(),
    }


def _events_record() -> dict[str, Any]:
    """Bounded-deque semantics: appends drop from the front, rebuilds truncate."""
    state = WorkerTermState()
    maxlen = state.events.maxlen
    assert maxlen is not None
    for seq in range(maxlen + 3):
        state.events.append({"seq": seq})

    rebuilt = deque(state.events, maxlen=4)
    return {
        "len_after_overflow": len(state.events),
        "first_seq": state.events[0]["seq"],
        "last_seq": state.events[-1]["seq"],
        "rebuilt_len": len(rebuilt),
        "rebuilt_seqs": [event["seq"] for event in rebuilt],
    }


def main() -> int:
    """Write the golden corpus and report the case count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_hub_models_golden.py",
        "now": NOW,
        "leases": _lease_record(),
        "state": _state_record(),
        "events": _events_record(),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(LEASE_CASES)} lease cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
