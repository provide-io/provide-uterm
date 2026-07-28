#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript fan-out port.

Fan-out drives several worker sessions from one command, so the group record
is a policy object: its defaults decide how long the hub waits for a session
to go quiet, how far outputs may drift before they count as divergent, and
whether one failure stops the rest.

The store's access rule is the security-relevant part and is recorded from
the reference: a group is visible to its creator *or* to anyone named in its
grants, and to nobody else. Getting that wrong either hides an operator's own
groups or shows them someone else's fleet.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_fanout_golden.py
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from provide.uterm.server.bridge.fanout._models import FanOutGroup, FanOutResult, SessionFanOutResult
from provide.uterm.server.bridge.fanout._store import InMemoryFanOutStore

OUT = Path(__file__).with_name("fanout_golden.json")

NOW = 1000.0


def _defaults(cls: Any) -> dict[str, Any]:
    """The default value of every optional field on a dataclass."""
    out: dict[str, Any] = {}
    for field in fields(cls):
        if field.default is not field.default_factory and repr(field.default) != "<factory>":
            try:
                out[field.name] = field.default
            except Exception:  # pragma: no cover - defensive
                continue
    return {name: value for name, value in out.items() if not repr(value).startswith("<")}


def _group(group_id: str, created_by: str, grants: list[str] | None = None) -> FanOutGroup:
    """A group owned by *created_by*, optionally shared."""
    return FanOutGroup(
        group_id=group_id,
        name=f"group {group_id}",
        worker_ids=["w1", "w2"],
        created_by=created_by,
        created_at=NOW,
        grants=list(grants or []),
    )


async def _store_record() -> dict[str, Any]:
    """Save, get, delete, and the visibility rule."""
    store = InMemoryFanOutStore()
    missing = await store.get("nope")

    await store.save(_group("g1", "alice"))
    await store.save(_group("g2", "bob", ["alice"]))
    await store.save(_group("g3", "bob"))
    await store.save(_group("g4", "carol", ["dave", "alice"]))

    alice = sorted(group.group_id for group in await store.list_for_principal("alice"))
    bob = sorted(group.group_id for group in await store.list_for_principal("bob"))
    carol = sorted(group.group_id for group in await store.list_for_principal("carol"))
    dave = sorted(group.group_id for group in await store.list_for_principal("dave"))
    stranger = sorted(group.group_id for group in await store.list_for_principal("eve"))

    # Saving the same id replaces rather than duplicating.
    await store.save(_group("g1", "alice", ["zoe"]))
    after_replace = await store.get("g1")

    await store.delete("g3")
    await store.delete("g3")  # a second delete is a no-op, not an error
    after_delete = sorted(group.group_id for group in await store.list_for_principal("bob"))

    return {
        "missing_is_none": missing is None,
        "alice": alice,
        "bob": bob,
        "carol": carol,
        "dave": dave,
        "stranger": stranger,
        "replaced_grants": list(after_replace.grants) if after_replace else None,
        "bob_after_delete": after_delete,
    }


def main() -> int:
    """Write the golden corpus and report what was recorded."""
    group = FanOutGroup(
        group_id="g1",
        name="fleet",
        worker_ids=["w1"],
        created_by="alice",
        created_at=NOW,
    )
    result = FanOutResult(
        group_id="g1",
        send_id="s1",
        command="uptime",
        sent_at=NOW,
        results=[],
        divergent_sessions=[],
        failed_sessions=[],
    )

    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_fanout_golden.py",
        "group_defaults": {
            "mode": group.mode,
            "stop_on_first_error": group.stop_on_first_error,
            "error_pattern": group.error_pattern,
            "quiesce_ms": group.quiesce_ms,
            "max_response_ms": group.max_response_ms,
            "divergence_threshold": group.divergence_threshold,
            "grants": list(group.grants),
        },
        "result_defaults": {
            "error": result.error,
            "approval_required": result.approval_required,
            "approval_id": result.approval_id,
        },
        "group_fields": [field.name for field in fields(FanOutGroup)],
        "result_fields": [field.name for field in fields(FanOutResult)],
        "session_result_fields": [field.name for field in fields(SessionFanOutResult)],
        "store": asyncio.run(_store_record()),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(payload['group_fields'])} group fields)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
