#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-server
"""Generate the differential golden corpus for the connection-profile store.

Saved connection targets, owned by whoever made them.

**An update may touch nine fields and no others.** The owner, the identifier,
the connector type and the creation time are not among them — a client that
could rewrite an owner could hand itself somebody else's saved credentials by
editing a profile rather than by asking for it.

**A listing shows what the caller owns plus what is shared.** Asking with no
owner at all returns everything, which is the administrative view; asking as
somebody returns theirs and the shared ones, never another person's private
target.

**A corrupt store is an error, not an empty list.** Returning nothing would
look like a user with no profiles and invite writing over what could not be
read.

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_profiles_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.server.profiles import _MUTABLE_FIELDS, ConnectionProfile

OUT = Path(__file__).with_name("profiles_golden.json")

CREATED = 1_700_000_000.0
UPDATED = 1_700_000_100.0


def _profile(profile_id: str, owner: str, visibility: str = "private", **extra: Any) -> ConnectionProfile:
    """One saved target."""
    return ConnectionProfile(
        profile_id=profile_id,
        owner=owner,
        name=f"{profile_id}-name",
        connector_type="ssh",
        visibility=visibility,  # type: ignore[arg-type]
        created_at=CREATED,
        updated_at=CREATED,
        **extra,
    )


PROFILES = [
    _profile("p1", "alice"),
    _profile("p2", "alice", "shared"),
    _profile("p3", "bob"),
    _profile("p4", "bob", "shared"),
]

# (name, updates) — what an update is allowed to change.
UPDATE_CASES: list[tuple[str, dict[str, Any]]] = [
    ("a name", {"name": "renamed"}),
    ("a host and port", {"host": "10.0.0.2", "port": 2222}),
    ("several at once", {"name": "n", "username": "u", "tags": ["a"], "input_mode": "hijack"}),
    ("recording and visibility", {"recording_enabled": True, "visibility": "shared"}),
    ("nothing at all", {}),
    # None of these may be changed.
    ("the owner", {"owner": "mallory"}),
    ("the identifier", {"profile_id": "stolen"}),
    ("the connector type", {"connector_type": "shell"}),
    ("the creation time", {"created_at": 0.0}),
    ("the update time", {"updated_at": 0.0}),
    ("a field nobody defined", {"nonsense": 1}),
    # A permitted change alongside a forbidden one: the permitted one lands.
    ("a name and an owner", {"name": "renamed", "owner": "mallory"}),
]


def _apply(profile: ConnectionProfile, updates: dict[str, Any]) -> dict[str, Any]:
    """The store's own update, with its clock pinned."""
    safe = {key: value for key, value in updates.items() if key in _MUTABLE_FIELDS}
    data = profile.model_dump(mode="python")
    data.update(safe)
    data["updated_at"] = UPDATED
    return ConnectionProfile.model_validate(data).model_dump(mode="json")


def _visible(owner: str | None) -> list[str]:
    """Which profiles a caller sees."""
    if owner is None:
        return [p.profile_id for p in PROFILES]
    return [p.profile_id for p in PROFILES if p.owner == owner or p.visibility == "shared"]


def _build() -> dict[str, Any]:
    """Everything the store decides."""
    return {
        "mutable_fields": sorted(_MUTABLE_FIELDS),
        "profiles": [p.model_dump(mode="json") for p in PROFILES],
        "listings": [{"owner": owner, "visible": _visible(owner)} for owner in (None, "alice", "bob", "carol", "")],
        "updates": [
            {"name": name, "updates": updates, "result": _apply(PROFILES[0], updates)} for name, updates in UPDATE_CASES
        ],
        "updated_at": UPDATED,
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(UPDATE_CASES)} updates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
