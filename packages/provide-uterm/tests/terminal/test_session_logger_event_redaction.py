#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``SessionLogger.log_event`` / ``log_control`` apply the recording redactor.

Keys, screens and wire text were always redacted, but named events were
written verbatim — so a detector annotation quoting the matched command line,
an operator annotation, or a ``runtime_error`` carrying ``str(exc)`` stored the
secret the screen beside it had redacted.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from provide.uterm.recording import InMemoryRecordingStore
from provide.uterm.redaction import make_redactor
from provide.uterm.session_logger import SessionLogger

_REDACTOR = make_redactor([r"S3cret\w*"])


async def _record(
    *,
    redactor: Any = None,
    mode: Any = "exclude",
    event: str = "annotation",
    data: dict[str, Any],
    control: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    store = InMemoryRecordingStore()
    logger = SessionLogger(store, redactor=redactor, control_channel_mode=mode, flush_interval_s=3600)
    await logger.start("s1")
    await logger.log_event(event, data)
    if control is not None:
        await logger.log_control("send", control)
    await logger.stop()
    return [e for e in await store.get_entries("s1", limit=500) if e["event"] not in ("log_start", "log_stop")]


async def test_log_event_redacts_top_level_string() -> None:
    entries = await _record(redactor=_REDACTOR, data={"description": "curl --data pw=S3cretValue https://"})
    assert entries[0]["data"] == {"description": "curl --data pw=[REDACTED] https://"}


async def test_log_event_redacts_nested_dicts_lists_and_tuples() -> None:
    data = {
        "span": {"note": "S3cret1", "inner": [{"x": "a S3cret2 b"}, "S3cret3"]},
        "items": ("keep", "S3cret4"),
    }
    entries = await _record(redactor=_REDACTOR, data=data)
    assert entries[0]["data"] == {
        "span": {"note": "[REDACTED]", "inner": [{"x": "a [REDACTED] b"}, "[REDACTED]"]},
        "items": ["keep", "[REDACTED]"],
    }
    # The caller's payload is not mutated in place.
    assert data["span"]["note"] == "S3cret1"


async def test_log_event_preserves_non_string_values_exactly() -> None:
    data = {"seq": 7, "permanent": True, "ratio": 0.5, "span": None, "nested": {"n": 0, "flag": False}}
    entries = await _record(redactor=_REDACTOR, data=data)
    written = entries[0]["data"]
    assert written == data
    assert type(written["seq"]) is int
    assert written["permanent"] is True
    assert written["span"] is None
    assert written["nested"]["flag"] is False


async def test_log_event_without_redactor_is_verbatim_but_not_aliased() -> None:
    """With redaction off the payload is recorded as-is, but as a copy.

    It used to buffer the caller's own dict, so a caller mutating it after
    ``log_event`` returned rewrote the record that had not been flushed yet.
    """
    store = InMemoryRecordingStore()
    logger = SessionLogger(store, redactor=None, flush_interval_s=3600)
    await logger.start("s1")
    data = {"description": "pw=S3cretValue", "seq": 1, "token": "abc"}
    await logger.log_event("annotation", data)
    data["description"] = "changed later"
    data["seq"] = 2
    await logger.stop()
    entry = next(e for e in await store.get_entries("s1", limit=500) if e["event"] == "annotation")
    assert entry["data"] is not data
    # Verbatim: no text rules and no key-aware masking without a redactor.
    assert entry["data"] == {"description": "pw=S3cretValue", "seq": 1, "token": "abc"}


async def test_log_event_redacts_runtime_error_text() -> None:
    entries = await _record(
        redactor=_REDACTOR, event="runtime_error", data={"error": "auth failed for S3cretToken", "permanent": True}
    )
    assert entries[0]["event"] == "runtime_error"
    assert entries[0]["data"] == {"error": "auth failed for [REDACTED]", "permanent": True}


async def test_log_control_redacts_decoded_frame_in_wire_mode() -> None:
    entries = await _record(
        redactor=_REDACTOR,
        mode="wire",
        data={},
        control={"type": "paste", "text": "S3cretPaste", "n": 3},
    )
    control = next(e for e in entries if e["event"] == "control_send")
    assert control["data"] == {"control": {"type": "paste", "text": "[REDACTED]", "n": 3}}


async def test_log_control_without_redactor_is_unchanged_but_copied() -> None:
    frame = {"type": "paste", "text": "S3cretPaste"}
    entries = await _record(redactor=None, mode="wire", data={}, control=frame)
    control = next(e for e in entries if e["event"] == "control_send")
    assert control["data"]["control"] == frame
    assert control["data"]["control"] is not frame


async def test_log_screen_without_redactor_is_copied() -> None:
    store = InMemoryRecordingStore()
    logger = SessionLogger(store, flush_interval_s=3600)
    await logger.start("s1")
    snap = {"screen": "abc", "cursor": [1, 2]}
    await logger.log_screen(snap, b"abc")
    snap["screen"] = "mutated"
    await logger.stop()
    read = next(e for e in await store.get_entries("s1", limit=500) if e["event"] == "read")
    assert read["data"]["screen"] == "abc"
    assert read["data"]["cursor"] == [1, 2]


# ---------------------------------------------------------------------------
# Key-aware redaction: credential fields the text rules cannot see
# ---------------------------------------------------------------------------


async def test_log_control_masks_a_resume_frame_token_by_field_name() -> None:
    """``{"type": "resume", "token": ...}`` carries no ``token=`` context for the text rules."""
    entries = await _record(
        redactor=_REDACTOR,
        mode="wire",
        data={},
        control={"type": "resume", "token": "rt_8f3a9c", "player_id": 2},
    )
    control = next(e for e in entries if e["event"] == "control_send")
    assert control["data"] == {"control": {"type": "resume", "token": "[SECRET_REDACTED]", "player_id": 2}}


async def test_log_control_masks_a_session_token_frame_and_a_hello_resume_token() -> None:
    store = InMemoryRecordingStore()
    logger = SessionLogger(store, redactor=_REDACTOR, control_channel_mode="wire", flush_interval_s=3600)
    await logger.start("s1")
    await logger.log_control("recv", {"type": "session_token", "token": "st_1", "player_id": None})
    await logger.log_control("recv", {"type": "hello", "resume_token": "rt_2", "resume_supported": True})
    await logger.log_control("recv", {"type": "hello", "resume_token": None})
    await logger.stop()
    frames = [e["data"]["control"] for e in await store.get_entries("s1", limit=500) if e["event"] == "control_recv"]
    assert frames == [
        {"type": "session_token", "token": "[SECRET_REDACTED]", "player_id": None},
        {"type": "hello", "resume_token": "[SECRET_REDACTED]", "resume_supported": True},
        {"type": "hello", "resume_token": None},
    ]


async def test_log_event_masks_nested_sensitive_keys() -> None:
    entries = await _record(
        redactor=_REDACTOR,
        data={"connector": {"host": "h", "Password": "hunter2", "headers": [{"Authorization": "Bearer x"}]}},
    )
    assert entries[0]["data"] == {
        "connector": {"host": "h", "Password": "[SECRET_REDACTED]", "headers": [{"Authorization": "[SECRET_REDACTED]"}]}
    }


# ---------------------------------------------------------------------------
# Depth: an inbound control frame is attacker-shaped
# ---------------------------------------------------------------------------


def _deep_frame(depth: int) -> dict[str, Any]:
    inner: Any = "S3cretDeep"
    for _ in range(depth):
        inner = {"a": inner}
    return {"type": "x", "payload": inner}


async def test_a_5000_deep_control_frame_is_recorded_without_recursion_error() -> None:
    """A ~10KB frame nested 5000 deep used to raise RecursionError out of log_control / log_event.

    The hosted runtime's ControlFrameDecoder rejects inbound frames nested past
    32 before they reach the recorder, but SessionLogger is public API and
    other callers hand it decoded JSON directly. Everything past the depth cap
    is replaced by the marker, and the record serialises.
    """
    depth = 5000
    # Built iteratively: json.loads / == on this would themselves recurse on
    # older interpreters, and the point is what the recorder does with it.
    frame = _deep_frame(depth)
    assert sys.getrecursionlimit() < depth

    store = InMemoryRecordingStore()
    logger = SessionLogger(store, redactor=_REDACTOR, control_channel_mode="wire", flush_interval_s=3600)
    await logger.start("s1")
    await logger.log_control("recv", frame)
    await logger.log_event("annotation", frame)
    await logger.stop()
    recorded = [e for e in await store.get_entries("s1", limit=500) if e["event"] in ("control_recv", "annotation")]
    assert len(recorded) == 2
    for entry in recorded:
        text = json.dumps(entry)
        assert "S3cretDeep" not in text
        assert "[NESTED_REDACTED]" in text


# ---------------------------------------------------------------------------
# Quota first: nothing is redacted for a record that would be dropped
# ---------------------------------------------------------------------------


async def test_nothing_is_redacted_once_the_quota_is_reached() -> None:
    calls: list[str] = []

    def counting(text: str) -> str:
        calls.append(text)
        return text

    store = InMemoryRecordingStore()
    logger = SessionLogger(store, max_bytes=1, redactor=counting, control_channel_mode="wire", flush_interval_s=3600)
    await logger.start("s1")
    logger._max_bytes = logger._bytes_written + 1  # room for exactly one more record
    await logger.log_event("annotation", {"d": "first"})  # crosses the 1-byte quota
    calls.clear()
    await logger.log_event("annotation", {"d": "dropped"})
    await logger.log_control("send", {"type": "paste", "text": "dropped"})
    await logger.log_screen({"screen": "dropped"}, b"dropped")
    await logger.log_wire("send", "dropped")
    await logger.log_send("dropped")
    await logger.stop()
    assert calls == []
    events = [e["event"] for e in await store.get_entries("s1", limit=500)]
    assert events.count("annotation") == 1
    assert logger._quota_warned is True


async def test_byte_accounting_counts_the_redacted_record() -> None:
    store = InMemoryRecordingStore()
    logger = SessionLogger(store, redactor=_REDACTOR, flush_interval_s=3600)
    await logger.start("s1")
    before = logger._bytes_written
    await logger.log_event("annotation", {"description": "S3cret" + "x" * 500})
    after = logger._bytes_written
    await logger.stop()
    record = next(e for e in await store.get_entries("s1", limit=500) if e["event"] == "annotation")
    assert record["data"]["description"] == "[REDACTED]"
    # The quota counter grows by exactly the serialized size of what was
    # buffered: the short redacted record, not the 500-byte secret passed in.
    assert after - before == len(json.dumps(record)) + 1


async def test_write_event_rechecks_the_quota_under_the_lock() -> None:
    """A caller can pass the entry check and still find the quota spent once it holds the lock."""
    store = InMemoryRecordingStore()
    logger = SessionLogger(store, max_bytes=10, flush_interval_s=3600)
    await logger.start("s1")
    logger._bytes_written = 10
    await logger._write_event("late", {"a": 1})
    await logger.stop()
    assert [e["event"] for e in await store.get_entries("s1", limit=500)] == ["log_start", "log_stop"]
    assert logger._quota_warned is True
