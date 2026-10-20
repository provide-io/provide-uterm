#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for a kept capture session's file.

``provide.uterm.server.capture_state`` writes one JSON file per PAM capture
session, and reads it back at startup only if it is a record of a format the
server reads. The Go port keeps the same files, so a server switched from one
port to the other picks up the sessions the first one kept. This records what
the reference writes for a live and a previous record, what it answers for each
file it refuses, and what it reads from the files another build of the same
format writes (a key it does not know, a defaulted key missing): the Go port
must read the records back to the same record, refuse the refusals with the
same words, and read the other builds' files to the same definition.

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-go/capturestate/testdata/gen_capture_record_golden.py
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from provide.uterm.server.capture_record import CaptureRecord, RecordError, parse_bytes, parse_record
from provide.uterm.server.config_schema import SessionDefinition

OUT = Path(__file__).with_name("capture_record_golden.json")

SESSION_ID = "pam-suokki-capture-4242-777"


def _record(kind: str) -> dict[str, Any]:
    definition = SessionDefinition(
        session_id=SESSION_ID,
        display_name="suokki (pts/3) [live]",
        connector_type="pty_capture",
        connector_config={"socket_path": "/run/uterm-capture/uterm-cap-4242.sock", "cols": 100},
        input_mode="hijack",
        tags=["pam", "capture", "suokki"],
        owner="blackbetty:7",
        visibility="private",
        created_at=datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC),
    )
    return CaptureRecord(
        definition=definition,
        recording_instance="0123456789abcdef0123456789abcdef",
        screen="\x1b[1mdeck\x1b[0m\nline two",
        cols=80,
        rows=24,
        scrollback=["older", "old"],
        saved_at=1760875200.5,
        ended_at=None if kind == "live" else 1760875260.25,
    ).to_json()


def _session(**changes: Any) -> dict[str, Any]:
    return _record("live")["session"] | changes


def _session_without(key: str) -> dict[str, Any]:
    return {name: value for name, value in _record("live")["session"].items() if name != key}


def _session_renamed(key: str, to: str) -> dict[str, Any]:
    return {to if name == key else name: value for name, value in _record("live")["session"].items()}


# (name, kind read as, the record of that kind with these keys replaced)
REFUSALS: list[tuple[str, str, Any]] = [
    ("not an object", "live", []),
    ("a format newer than this server reads", "live", {"format": 2}),
    ("a format older than any", "live", {"format": 0}),
    ("the other kind", "previous", {"kind": "live"}),
    ("no session", "live", {"session": None}),
    ("a session without its visibility", "live", {"session": _session_without("visibility")}),
    ("a session without its owner", "live", {"session": _session_without("owner")}),
    ("a session without its connector config", "live", {"session": _session_without("connector_config")}),
    ("a session without its connector type", "live", {"session": _session_without("connector_type")}),
    ("a session without its id", "live", {"session": _session_without("session_id")}),
    ("a session key in another case", "live", {"session": _session_renamed("session_id", "Session_ID")}),
    ("a session whose id is not text", "live", {"session": _session(session_id=7)}),
    ("a connector config that is a list", "live", {"session": _session(connector_config=[])}),
    ("an input mode outside its set", "live", {"session": _session(input_mode="sideways")}),
    ("an input mode written as a number", "live", {"session": _session(input_mode=1)}),
    ("auto start written as text", "live", {"session": _session(auto_start="true")}),
    ("tags written as null", "live", {"session": _session(tags=None)}),
    ("tags with a number in them", "live", {"session": _session(tags=["pam", 1])}),
    ("recording written as a number", "live", {"session": _session(recording_enabled=1)}),
    ("a creation time that is only a date", "live", {"session": _session(created_at="2026-10-19")}),
    ("a creation time on a day that does not exist", "live", {"session": _session(created_at="2026-02-30T00:00:00Z")}),
    ("an owner that is a number", "live", {"session": _session(owner=7)}),
    ("a visibility outside its set", "live", {"session": _session(visibility="everyone")}),
    ("ephemeral written as a number", "live", {"session": _session(ephemeral=0)}),
    ("presence written as null", "live", {"session": _session(presence=None)}),
    ("an idle timeout written as a float", "live", {"session": _session(auto_transfer_idle_s=30.0)}),
    ("an idle timeout past 64 bits", "live", {"session": _session(auto_transfer_idle_s=2**63)}),
    ("a keystroke queue outside its set", "live", {"session": _session(keystroke_queue="later")}),
    ("a format written as a float", "live", {"format": 1.0}),
    ("a format written as true", "live", {"format": True}),
    ("a login pid written as a float", "live", {"login": {"pid": 4242.0, "start_ticks": 777}}),
    ("a login with another key", "live", {"login": {"pid": 4242, "start_ticks": 777, "uid": 0}}),
    ("a screen whose text is null", "live", {"screen": {"ansi": None, "cols": 80, "rows": 24}}),
    ("a screen wider than 64 bits", "live", {"screen": {"ansi": "s", "cols": 2**63, "rows": 24}}),
    ("scrollback with null in it", "live", {"scrollback": ["a", None]}),
    ("another session's id", "live", {"session": _session(session_id="pam-x-capture-1-2")}),
    ("not a capture session", "live", {"session": _session(connector_type="pty")}),
    ("a login with another start", "live", {"login": {"pid": 4242, "start_ticks": 778}}),
    ("no login", "live", {"login": None}),
    ("an empty recording instance", "live", {"recording_instance": ""}),
    ("a numeric recording instance", "live", {"recording_instance": 7}),
    ("a screen without rows", "live", {"screen": {"ansi": "s", "cols": 80}}),
    ("a screen that is not text", "live", {"screen": {"ansi": 1, "cols": 80, "rows": 24}}),
    ("a screen with a boolean width", "live", {"screen": {"ansi": "s", "cols": True, "rows": 24}}),
    ("a screen with a fractional height", "live", {"screen": {"ansi": "s", "cols": 80, "rows": 24.5}}),
    ("a screen with no width", "live", {"screen": {"ansi": "s", "cols": 0, "rows": 24}}),
    ("a screen that is a string", "live", {"screen": "s"}),
    ("scrollback that is a string", "live", {"scrollback": "a"}),
    ("scrollback with a number in it", "live", {"scrollback": [1]}),
    ("no scrollback", "live", {"scrollback": None}),
    ("a negative save time", "live", {"saved_at": -1}),
    ("a save time written as text", "live", {"saved_at": "5"}),
    ("a save time past any float", "live", {"saved_at": 10**400}),
    ("an end time past any float", "previous", {"ended_at": 10**400}),
    ("a live record that ended", "live", {"ended_at": 6.0}),
    ("a previous record that never ended", "previous", {"ended_at": None}),
]


def _refusal(name: str, kind: str, changes: Any) -> dict[str, Any]:
    data: Any = _record(kind) | changes if isinstance(changes, dict) else changes
    try:
        parse_record(data, session_id=SESSION_ID, kind=kind)
    except RecordError as exc:
        return {"name": name, "kind": kind, "data": data, "error": str(exc)}
    raise AssertionError(f"{name}: accepted")


# (name, the session of a live record with these keys dropped, and these added):
# files another build of the same format writes, which this one reads. A key
# with a default takes it; a key this build does not know is ignored.
ACCEPTED: list[tuple[str, tuple[str, ...], dict[str, Any]]] = [
    ("a file from before its keystroke queue", ("keystroke_queue",), {}),
    (
        "a file from before every defaulted key but its creation time",
        (
            "display_name",
            "input_mode",
            "auto_start",
            "tags",
            "recording_enabled",
            "ephemeral",
            "presence",
            "auto_transfer_idle_s",
            "keystroke_queue",
        ),
        {},
    ),
    ("a file from a build with a key this one does not know", (), {"surprise": {"nested": [1]}}),
    ("a key in another case is not the key", (), {"Owner": "someone-else"}),
]


def _accepted(name: str, dropped: tuple[str, ...], added: dict[str, Any]) -> dict[str, Any]:
    session = {key: value for key, value in _record("live")["session"].items() if key not in dropped} | added
    data = _record("live") | {"session": session}
    record = parse_record(data, session_id=SESSION_ID, kind="live")
    return {
        "name": name,
        "data": data,
        "session": record.to_json()["session"],
        "ignored_keys": list(record.ignored_keys),
    }


# (name, bytes) a file can hold that are not a JSON object at all.
BAD_FILES: list[tuple[str, bytes]] = [
    ("not JSON", b"{not json"),
    ("not UTF-8", b'{"format": "\xc3\x28"}'),
    ("a JSON array", b"[]"),
    ("a NaN", b'{"format": NaN}'),
    ("an Infinity", b'{"saved_at": Infinity}'),
    ("nested past any parser's depth", b"[" * 20_000),
]


def _bad_file(name: str, content: bytes) -> dict[str, Any]:
    try:
        parse_bytes(content, session_id=SESSION_ID, kind="live")
    except RecordError as exc:
        return {"name": name, "content_b64": base64.b64encode(content).decode("ascii"), "error": str(exc)}
    raise AssertionError(f"{name}: accepted")


def main() -> int:
    corpus = {
        "session_id": SESSION_ID,
        "records": {kind: _record(kind) for kind in ("live", "previous")},
        "refusals": [_refusal(*case) for case in REFUSALS],
        "accepted": [_accepted(*case) for case in ACCEPTED],
        "bad_files": [_bad_file(*case) for case in BAD_FILES],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['refusals'])} refusals)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
