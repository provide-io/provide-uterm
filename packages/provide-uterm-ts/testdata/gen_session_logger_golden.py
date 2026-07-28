#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``sessionLogger`` port.

Records the shape of every entry the logger writes. Wall-clock ``ts`` fields
are stripped, since they are fresh by design; everything else — the base64
payloads, the encodings, the redaction and the context — is pinned.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_session_logger_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.redaction import make_redactor

from provide.uterm.recording import InMemoryRecordingStore
from provide.uterm.session_logger import SessionLogger

OUT = Path(__file__).with_name("session_logger_golden.json")


def _strip(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the fresh-by-design timestamps."""
    cleaned = []
    for entry in entries:
        item = dict(entry)
        item.pop("ts", None)
        if item.get("event") == "log_start":
            item["data"] = {"stripped": True}
        cleaned.append(item)
    return cleaned


async def _drive(**kwargs: Any) -> list[dict[str, Any]]:
    """Run one logger through a fixed script and return what it wrote."""
    store = InMemoryRecordingStore()
    logger = SessionLogger(store, flush_interval_s=3600, **kwargs)
    await logger.start("s1")
    await logger.log_send("ls -la\r")
    await logger.log_send_masked(8)
    await logger.log_screen({"screen": "hello", "cursor": {"x": 1}}, b"raw\xff")
    await logger.log_event("custom", {"a": 1})
    await logger.log_wire("send", "wire out")
    await logger.log_wire("recv", "wire in")
    await logger.log_control("send", {"type": "hello"})
    await logger.log_control("recv", {"type": "hello_ack"})
    logger.set_context({"worker": "w1", "n": 2})
    await logger.log_event("with_context", {})
    logger.clear_context()
    await logger.log_event("without_context", {})
    await logger.flush()
    await logger.stop()
    return _strip(await store.get_entries("s1", limit=500))


async def _quota_record() -> dict[str, Any]:
    """A logger past its byte quota stops writing."""
    store = InMemoryRecordingStore()
    logger = SessionLogger(store, max_bytes=1, flush_interval_s=3600)
    await logger.start("s1")
    await logger.log_event("first", {"a": 1})
    await logger.log_event("second", {"a": 2})
    await logger.flush()
    await logger.stop()
    return {"entries": _strip(await store.get_entries("s1", limit=500))}


async def _batch_record() -> dict[str, Any]:
    """A full batch flushes without waiting for the interval."""
    store = InMemoryRecordingStore()
    logger = SessionLogger(store, batch_size=2, flush_interval_s=3600)
    await logger.start("s1")
    await logger.log_event("a", {})
    before = len(await store.get_entries("s1", limit=500))
    await logger.log_event("b", {})
    after = len(await store.get_entries("s1", limit=500))
    await logger.stop()
    return {"after_one": before, "after_two": after}


async def _run() -> dict[str, Any]:
    """Build every section of the corpus."""
    redactor = make_redactor([r"secret\w*"])
    return {
        "exclude_mode": await _drive(),
        "wire_mode": await _drive(control_channel_mode="wire"),
        "redacted": await _drive(control_channel_mode="wire", redactor=redactor),
        "quota": await _quota_record(),
        "batch": await _batch_record(),
    }


def main() -> int:
    """Write the golden corpus and report the record count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_session_logger_golden.py",
        **asyncio.run(_run()),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(payload['exclude_mode'])} exclude-mode entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
