#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``recording`` port.

The interesting behaviour is pagination: ``get_entries`` means two different
things depending on whether an offset is given, and the limit is normalised
through a clamp that treats zero specially. Both stores must agree, and this
corpus drives them side by side.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_recording_golden.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from provide.uterm.recording import (
    InMemoryRecordingStore,
    LocalFileRecordingStore,
    NullRecordingStore,
    _normalize_limit,
)

OUT = Path(__file__).with_name("recording_golden.json")

# The events appended after the automatic log_start.
#
# The timestamps are deliberately non-integral. A whole-valued float is
# indistinguishable from an int once it has been through JSON and into a
# JavaScript number, so a byte-size comparison across the two runtimes would
# be measuring that ambiguity rather than the store.
EVENTS: list[dict[str, Any]] = [
    {"ts": 1.5, "event": "read", "data": {"n": 1}},
    {"ts": 2.5, "event": "write", "data": {"n": 2}},
    {"ts": 3.5, "event": "read", "data": {"n": 3}},
    {"ts": 4.5, "event": "write", "data": {"n": 4}},
    {"ts": 5.5, "event": "read", "data": {"n": 5}},
]

# (limit, offset, event) query shapes.
QUERIES: list[tuple[int, int | None, str | None]] = [
    (200, None, None),
    (2, None, None),
    (2, 0, None),
    (2, 1, None),
    (2, 99, None),
    (2, -1, None),
    (200, None, "read"),
    (2, None, "read"),
    (2, 1, "read"),
    (200, None, "nosuch"),
    # Zero means the default of 200, and the clamp bounds the rest.
    (0, None, None),
    (1, None, None),
    (-5, None, None),
    (99999, None, None),
]

# Inputs for the limit clamp on its own.
LIMIT_INPUTS = [0, 1, 2, 200, 499, 500, 501, 99999, -1, -500]


async def _drive(store: Any, session_id: str) -> None:
    """Run one store through the recording lifecycle."""
    await store.start_session(session_id, {"kind": "corpus"})
    await store.append_events(session_id, EVENTS)


def _strip_volatile(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the wall-clock timestamps the lifecycle events carry."""
    cleaned = []
    for entry in entries:
        item = dict(entry)
        if item.get("event") in {"log_start", "log_stop"}:
            item.pop("ts", None)
        cleaned.append(item)
    return cleaned


async def _run() -> dict[str, Any]:
    """Build every section of the corpus."""
    memory = InMemoryRecordingStore()
    await _drive(memory, "s1")

    with tempfile.TemporaryDirectory() as tmp:
        file_store = LocalFileRecordingStore(tmp)
        await _drive(file_store, "s1")

        queries = []
        for limit, offset, event in QUERIES:
            mem = await memory.get_entries("s1", limit=limit, offset=offset, event=event)
            fil = await file_store.get_entries("s1", limit=limit, offset=offset, event=event)
            queries.append(
                {
                    "limit": limit,
                    "offset": offset,
                    "event": event,
                    "memory": _strip_volatile(mem),
                    "file": _strip_volatile(fil),
                }
            )

        await file_store.end_session("s1")
        after_end = _strip_volatile(await file_store.get_entries("s1", limit=200))
        file_meta_exists = await file_store.recording_meta("s1")
        file_meta_missing = await file_store.recording_meta("nosuch")
        file_path_present = await file_store.get_path("s1") is not None
        file_path_missing = await file_store.get_path("nosuch") is None

    await memory.end_session("s1")
    memory_after_end = _strip_volatile(await memory.get_entries("s1", limit=200))
    memory_meta = await memory.recording_meta("s1")
    memory_meta_missing = await memory.recording_meta("nosuch")

    # A store carrying only the fixed events: its size is reproducible,
    # unlike one that also holds a wall-clock log_start timestamp.
    deterministic = InMemoryRecordingStore()
    await deterministic.append_events("s1", EVENTS)
    deterministic_meta = await deterministic.recording_meta("s1")

    null = NullRecordingStore()
    await null.start_session("s1", {"kind": "corpus"})
    await null.append_events("s1", EVENTS)
    await null.end_session("s1")

    return {
        "events": EVENTS,
        "queries": queries,
        "memory_after_end": memory_after_end,
        "file_after_end": after_end,
        "memory_meta": {"session_id": memory_meta["session_id"], "exists": memory_meta["exists"]},
        "deterministic_meta": deterministic_meta,
        "memory_meta_missing": memory_meta_missing,
        "file_meta_exists_keys": sorted(file_meta_exists.keys()),
        "file_meta_exists": {"session_id": file_meta_exists["session_id"], "exists": file_meta_exists["exists"]},
        "file_meta_missing": {
            "session_id": file_meta_missing["session_id"],
            "exists": file_meta_missing["exists"],
            "size_bytes": file_meta_missing["size_bytes"],
            "path": file_meta_missing["path"],
        },
        "file_path_present": file_path_present,
        "file_path_missing": file_path_missing,
        "null_meta": await null.recording_meta("s1"),
        "null_entries": await null.get_entries("s1"),
        "null_path": await null.get_path("s1"),
        "limits": [{"input": value, "normalized": _normalize_limit(value)} for value in LIMIT_INPUTS],
    }


def main() -> int:
    """Write the golden corpus and report the record count."""
    payload = {"generator": "packages/provide-uterm-ts/testdata/gen_recording_golden.py", **asyncio.run(_run())}
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(payload['queries'])} queries, {len(payload['limits'])} limits)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
