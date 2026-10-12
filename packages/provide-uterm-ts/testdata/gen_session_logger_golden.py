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

from provide.uterm.recording import InMemoryRecordingStore
from provide.uterm.redaction import make_redactor
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


# Entries whose compact and CPython-default JSON differ by far more than a
# timestamp's digits can: a separator per list element, and two characters
# ``ensure_ascii`` writes as escapes. A port that measured entries any other
# way than ``len(json.dumps(record)) + 1`` stops at a different entry.
BOUNDARY_PAYLOAD: dict[str, Any] = {"v": [0] * 100, "s": "caf\u00e9 \u2603"}

# A wall-clock-shaped timestamp of typical width (17 characters as JSON), used
# only to SIZE the boundary quota so that the recorded value is reproducible.
REPRESENTATIVE_TS = 1_700_000_000.123456


async def _quota_boundary_record() -> dict[str, Any]:
    """A byte quota that runs out partway through the third entry."""
    # The quota is sized from a FIXED representative timestamp, never from
    # time.time(). ``repr`` of a float is its shortest round-tripping form, so
    # time.time() serialises to anywhere from ~12 to 18 characters depending on
    # the instant (1760106000.5 vs 1760106000.1234567). Sizing from a live clock
    # made the recorded ``max_bytes`` differ between two runs of this generator
    # by a byte or two, which .ci/check_goldens.sh rightly flagged as
    # non-deterministic. The live run below still stamps real timestamps; the
    # half-entry margin is what absorbs their varying width.
    sample = {"ts": REPRESENTATIVE_TS, "event": "e", "data": BOUNDARY_PAYLOAD, "session_id": "s1"}
    size = len(json.dumps(sample)) + 1
    # Mirrors the ``log_start`` entry InMemoryRecordingStore.start_session writes
    # (and which recording_meta counts toward size_bytes).
    log_start = {
        "ts": REPRESENTATIVE_TS,
        "event": "log_start",
        "data": {"started_at": REPRESENTATIVE_TS},
        "session_id": "s1",
    }
    start = len(json.dumps(log_start)) + 1
    # Two entries fit with half an entry to spare, so the third is written and
    # the fourth is not. Half an entry is far wider than a timestamp's spread.
    max_bytes = start + 2 * size + size // 2

    store = InMemoryRecordingStore()
    logger = SessionLogger(store, max_bytes=max_bytes, flush_interval_s=3600)
    await logger.start("s1")
    for _ in range(6):
        await logger.log_event("e", BOUNDARY_PAYLOAD)
    await logger.stop()
    entries = await store.get_entries("s1", limit=500)
    return {
        "max_bytes": max_bytes,
        "payload": BOUNDARY_PAYLOAD,
        "attempts": 6,
        "written": sum(1 for entry in entries if entry["event"] == "e"),
    }


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


def _nest(depth: int, leaf: Any, *, container: str) -> Any:
    """Wrap *leaf* in *depth* nested dicts or lists (iteratively)."""
    value = leaf
    for _ in range(depth):
        value = {"k": value} if container == "dict" else [value]
    return value


# Caller payloads for log_event / log_control, replayed verbatim by the ports.
# They cover string redaction at every level, key-aware masking (a bare
# {"token": ...} has no ``token=`` context for the text rules), a null
# credential staying null, and the depth cap from both sides: a container 31
# levels down is walked, one at 32 becomes the marker. ``data`` is the payload
# itself; the root payload dict is level 0.
PAYLOAD_CALLS: list[dict[str, Any]] = [
    {
        "method": "event",
        "name": "annotation",
        "data": {
            "label": "rotate",
            "description": "curl --data secretPW https://x",
            "severity": "info",
            "span": {"from_seq": 1, "to_seq": 2},
            "notes": ["a secret1 b", {"inner": "secret2"}, 3, 0.5, True, None],
        },
    },
    {
        "method": "event",
        "name": "credentials",
        "data": {
            "token": "abc",
            "Resume-Token": "rt",
            "client_secret": "cs",
            "api_key": 42,
            "password": None,
            "Authorization": {"scheme": "Bearer"},
            "tokens": "secret3",
            "max_tokens": 5,
            "nested": [{"session_token": "st", "keep": "plain"}],
        },
    },
    {
        "method": "event",
        "name": "key_rule",
        # camelCase / header-style names, pairs, and the describe-not-hold
        # suffixes that keep a field visible (see provide.uterm.redaction).
        "data": {
            "accessToken": "at",
            "clientSecret": "cs",
            "privateKey": "pk",
            "Set-Cookie": "sid=1",
            "Proxy-Authorization": "Basic x",
            "credentials": {"user": "u"},
            "aws_secret_access_key": "k",
            "APIKey": "k2",
            "x-api-key": "k3",
            "access_key_id": "AKIA",
            "HTTPCookie": "c",
            "refresh.token": "rt",
            "token_count": 3,
            "token_type": "Bearer",
            "password_required": True,
            "secret_hash": "h",
            "session_id": "s-1",
            "secretary": "plain",
            "tokenizer": "bpe",
        },
    },
    {"method": "control", "direction": "recv", "data": {"type": "resume", "token": "rt_8f3a", "player_id": 2}},
    {"method": "control", "direction": "send", "data": {"type": "session_token", "token": "st_1", "player_id": None}},
    {
        "method": "control",
        "direction": "recv",
        "data": {"type": "hello", "resume_token": "rt_2", "capabilities": {"x": ["secret4"]}},
    },
    # payload is level 1, so 31 more containers put the innermost at level 31.
    {"method": "event", "name": "at_cap", "data": {"payload": _nest(31, "secret5", container="dict")}},
    {"method": "event", "name": "past_cap_dict", "data": {"payload": _nest(32, "secret6", container="dict")}},
    {"method": "control", "direction": "recv", "data": {"type": "x", "list": _nest(34, "secret7", container="list")}},
]


async def _payload_record(redactor: Any) -> list[dict[str, Any]]:
    """Replay PAYLOAD_CALLS through a wire-mode logger."""
    store = InMemoryRecordingStore()
    logger = SessionLogger(store, flush_interval_s=3600, control_channel_mode="wire", redactor=redactor)
    await logger.start("s1")
    for call in PAYLOAD_CALLS:
        if call["method"] == "event":
            await logger.log_event(call["name"], call["data"])
        else:
            await logger.log_control(call["direction"], call["data"])
    await logger.stop()
    return _strip(await store.get_entries("s1", limit=500))


async def _run() -> dict[str, Any]:
    """Build every section of the corpus."""
    redactor = make_redactor([r"secret\w*"])
    return {
        "exclude_mode": await _drive(),
        "wire_mode": await _drive(control_channel_mode="wire"),
        "redacted": await _drive(control_channel_mode="wire", redactor=redactor),
        "quota": await _quota_record(),
        "quota_boundary": await _quota_boundary_record(),
        "batch": await _batch_record(),
        "payloads": {
            "patterns": [r"secret\w*"],
            "calls": PAYLOAD_CALLS,
            "redacted": await _payload_record(redactor),
            "verbatim": await _payload_record(None),
        },
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
