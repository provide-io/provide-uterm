#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript bridge port.

Two pieces, both of which every port has to agree on exactly.

``policy`` is the shared authorization matrix — the same one the Go port
implements and ``spec/behavior.json`` documents. It answers "may this role do
this, holding this lease, on a session in this state", and the *error strings*
are as much of the contract as the verdicts: callers match on them. The whole
matrix is enumerated rather than sampled, so a port cannot agree on the common
cases and diverge in a corner.

``HijackCoordinator`` is the single-session lease state machine the Cloudflare
Durable Object uses directly. Two details in it are easy to lose: an acquire
by the *same* owner is a renewal that still mints a fresh hijack id, so the
caller always holds an authoritative token; and release checks only the id,
not expiry, so a lapsed lease can still be cleaned up by whoever held it.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_bridge_golden.py
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any
from unittest import mock

from provide.uterm.bridge.coordinator import HijackCoordinator
from provide.uterm.bridge.policy import ROLE_RANK, can_inject, can_perform, role_rank

OUT = Path(__file__).with_name("bridge_golden.json")

NOW = 1000.0

OPERATIONS = ["input_inject", "hijack_step", "hijack_release", "hijack_acquire", "unknown_op", ""]
ROLES = ["viewer", "operator", "admin", "root", ""]

# (name, requested lease seconds) for the coordinator's clamp.
LEASE_CASES: list[tuple[str, int]] = [
    ("below the floor", 0),
    ("negative", -10),
    ("at the floor", 1),
    ("typical", 90),
    ("at the ceiling", 3600),
    ("above the ceiling", 3601),
    ("absurd", 10**9),
]


def _policy_record() -> list[dict[str, Any]]:
    """Every operation against every role, lease and session state."""
    records = []
    for op, role, lease_owned, session_active in itertools.product(OPERATIONS, ROLES, [True, False], [True, False]):
        records.append(
            {
                "op": op,
                "role": role,
                "lease_owned": lease_owned,
                "session_active": session_active,
                "error": can_perform(op, role=role, lease_owned=lease_owned, session_active=session_active),
            }
        )
    return records


def _inject_record() -> list[dict[str, Any]]:
    """can_inject, which deliberately ignores the session id."""
    records = []
    for role, lease_id in itertools.product(ROLES, ["lease-1", ""]):
        records.append(
            {
                "role": role,
                "lease_id": lease_id,
                "error": can_inject("session-1", lease_id, role),
                # The session id must never gate the decision; a different one
                # has to give the same answer.
                "error_other_session": can_inject("session-2", lease_id, role),
            }
        )
    return records


def _describe(result: Any) -> dict[str, Any]:
    """Snapshot a result immediately.

    The session is mutated in place by later heartbeats, so this has to run at
    the moment of the operation — describing everything at the end would
    record the final expiry for every step.
    """
    return {
        "ok": result.ok,
        "error": result.error,
        "is_renewal": result.is_renewal,
        "has_session": result.session is not None,
        "owner": None if result.session is None else result.session.owner,
        "expires_at": None if result.session is None else result.session.lease_expires_at,
    }


def _coordinator_record() -> dict[str, Any]:
    """The acquire → heartbeat → release state machine."""
    fresh = HijackCoordinator()
    first_result = fresh.acquire("alice", 90, now=NOW)
    first = _describe(first_result)
    renewal_result = fresh.acquire("alice", 90, now=NOW + 10)
    renewal = _describe(renewal_result)
    contested = _describe(fresh.acquire("bob", 90, now=NOW + 20))

    # A lease that has lapsed frees the slot for anyone.
    lapsed = HijackCoordinator()
    lapsed.acquire("alice", 1, now=NOW)
    after_expiry = _describe(lapsed.acquire("bob", 90, now=NOW + 2))

    beats = HijackCoordinator()
    held = beats.acquire("alice", 90, now=NOW)
    assert held.session is not None
    hijack_id = held.session.hijack_id
    good_beat = _describe(beats.heartbeat(hijack_id, 120, now=NOW + 5))
    wrong_id = _describe(beats.heartbeat("not-the-id", 120, now=NOW + 6))
    wrong_owner = _describe(beats.heartbeat(hijack_id, 120, owner="bob", now=NOW + 7))
    right_owner = _describe(beats.heartbeat(hijack_id, 120, owner="alice", now=NOW + 8))

    idle = HijackCoordinator()
    beat_when_idle = _describe(idle.heartbeat("anything", 90, now=NOW))
    release_when_idle = _describe(idle.release("anything"))

    releases = HijackCoordinator()
    rel_held = releases.acquire("alice", 90, now=NOW)
    assert rel_held.session is not None
    wrong_release = _describe(releases.release("not-the-id"))
    good_release = _describe(releases.release(rel_held.session.hijack_id))
    double_release = _describe(releases.release(rel_held.session.hijack_id))

    # Release does not consult expiry: a lapsed lease is still cleanable by
    # whoever holds its id.
    stale = HijackCoordinator()
    stale_held = stale.acquire("alice", 1, now=NOW)
    assert stale_held.session is not None
    stale_release = _describe(stale.release(stale_held.session.hijack_id))

    # can_send_input reads the clock itself rather than taking a `now`, so it
    # has to be driven with a stubbed one — otherwise every lease acquired at
    # a synthetic NOW reads as long expired.
    gate = HijackCoordinator()
    gate_held = gate.acquire("alice", 90, now=NOW)
    assert gate_held.session is not None
    with mock.patch("time.monotonic", return_value=NOW + 1):
        can_send_with_id = gate.can_send_input(gate_held.session.hijack_id)
        can_send_wrong_id = gate.can_send_input("nope")
        can_send_no_id = gate.can_send_input(None)
        can_send_when_idle = HijackCoordinator().can_send_input("anything")
    # Once the lease lapses the gate closes, whatever id the caller quotes.
    with mock.patch("time.monotonic", return_value=NOW + 1000):
        can_send_after_expiry = gate.can_send_input(gate_held.session.hijack_id)

    return {
        "first": first,
        "renewal": renewal,
        "renewal_mints_new_id": (
            first_result.session is not None
            and renewal_result.session is not None
            and first_result.session.hijack_id != renewal_result.session.hijack_id
        ),
        "contested": contested,
        "after_expiry": after_expiry,
        "good_beat": good_beat,
        "wrong_id": wrong_id,
        "wrong_owner": wrong_owner,
        "right_owner": right_owner,
        "beat_when_idle": beat_when_idle,
        "release_when_idle": release_when_idle,
        "wrong_release": wrong_release,
        "good_release": good_release,
        "double_release": double_release,
        "stale_release": stale_release,
        "can_send_with_id": can_send_with_id,
        "can_send_wrong_id": can_send_wrong_id,
        "can_send_no_id": can_send_no_id,
        "can_send_when_idle": can_send_when_idle,
        "can_send_after_expiry": can_send_after_expiry,
    }


def _lease_clamp_record() -> list[dict[str, Any]]:
    """The coordinator's own lease bounds, which differ from the hub's."""
    records = []
    for name, requested in LEASE_CASES:
        coordinator = HijackCoordinator()
        result = coordinator.acquire("alice", requested, now=NOW)
        assert result.session is not None
        records.append(
            {
                "name": name,
                "requested": requested,
                "granted_seconds": result.session.lease_expires_at - NOW,
            }
        )
    return records


def main() -> int:
    """Write the golden corpus and report the case count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_bridge_golden.py",
        "now": NOW,
        "role_ranks": dict(ROLE_RANK),
        "unknown_role_rank": role_rank("nonsense"),
        "policy": _policy_record(),
        "inject": _inject_record(),
        "coordinator": _coordinator_record(),
        "lease_clamps": _lease_clamp_record(),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(payload['policy'])} policy cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
