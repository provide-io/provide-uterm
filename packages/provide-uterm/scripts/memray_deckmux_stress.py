# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DeckMux presence stress script for memray profiling.

Drives the full per-connection presence cycle that ``DeckMuxMixin``
exercises on every browser connect:

    generate_name -> generate_color -> generate_initials ->
    PresenceStore.add -> PresenceStore.update -> to_dict ->
    get_sync_payload -> PresenceStore.remove

Workload: 1_000 connections, each going through the full cycle.
Run via: python -m memray run -o deckmux_stress.bin scripts/memray_deckmux_stress.py
"""

from __future__ import annotations

from provide.uterm.deckmux import PresenceStore, generate_color, generate_name
from provide.uterm.deckmux._names import generate_initials

NUM_CONNECTIONS = 1_000


def run() -> None:
    store = PresenceStore()
    config = {"auto_transfer_idle_s": 30, "keystroke_queue": "display"}
    roles = ("viewer", "operator", "admin")

    for i in range(NUM_CONNECTIONS):
        user_id = f"user-{i:06d}"

        # Identity generation — hashes the connection id, picks adj/animal,
        # picks a colour avoiding taken ones, derives initials.
        name = generate_name(user_id)
        color = generate_color(user_id, store.taken_colors())
        initials = generate_initials(name)

        # Add presence, then drive an update (the broadcast hot path runs
        # this on every keystroke / scroll event).
        store.add(user_id, name, color, roles[i % len(roles)], initials)
        store.update(
            user_id,
            scroll_line=i,
            scroll_range=(0, i % 1024),
            total_lines=i % 4096,
            typing=(i % 2 == 0),
            cols=120,
            rows=40,
        )

        # Build a sync payload — this is what's broadcast when anyone joins.
        # We serialise on every connection so the test reflects the actual
        # per-connect cost.
        _ = store.get_sync_payload(config)

        # 25% of connections churn out (browser closed) — exercises remove()
        # and keeps the store size bounded so per-connection work doesn't
        # drift quadratically with iteration count.
        if i % 4 == 0:
            store.remove(user_id)


if __name__ == "__main__":
    run()
