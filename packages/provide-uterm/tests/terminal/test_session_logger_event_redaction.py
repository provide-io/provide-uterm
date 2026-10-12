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
from typing import Any

from provide.uterm.redaction import make_redactor

from provide.uterm.recording import InMemoryRecordingStore
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


async def test_log_event_without_redactor_is_unchanged_and_not_copied() -> None:
    data = {"description": "pw=S3cretValue", "seq": 1}
    entries = await _record(redactor=None, data=data)
    assert entries[0]["data"] is data
    assert entries[0]["data"] == {"description": "pw=S3cretValue", "seq": 1}


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


async def test_log_control_without_redactor_is_unchanged() -> None:
    frame = {"type": "paste", "text": "S3cretPaste"}
    entries = await _record(redactor=None, mode="wire", data={}, control=frame)
    control = next(e for e in entries if e["event"] == "control_send")
    assert control["data"]["control"] is frame


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
