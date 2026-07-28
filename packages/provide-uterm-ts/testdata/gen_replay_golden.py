#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript replay port.

A replay is what an incident review actually watches, so two things matter and
both are quiet when wrong:

* **Which frames are shown.** A frame is skipped when its event is not wanted
  *or* when it has no screen at all — but a screen that is the empty string is
  a real frame: a cleared terminal. Confusing "absent" with "empty" drops the
  moment the operator cleared the screen, which is often the moment before the
  interesting one.
* **The timing.** Delays come from the log's own timestamps divided by the
  speed multiplier, which is clamped at both ends. Without the clamp a speed
  of zero divides by zero and a negative one plays backwards; without the
  positive check a log whose timestamps go backwards sleeps for a negative
  time.

The corpus is recorded by driving the real functions with the sleep and the
output captured, so what is pinned is the reference's frame selection and its
delay schedule rather than a reading of them.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_replay_golden.py
"""

from __future__ import annotations

import base64
import io
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from provide.uterm.replay import raw as raw_module
from provide.uterm.replay import viewer as viewer_module

OUT = Path(__file__).with_name("replay_golden.json")

# A log with every shape the reader has to cope with.
LOG_RECORDS: list[Any] = [
    {"event": "screen", "ts": 100.0, "data": {"screen": "first"}},
    # No timestamp: the previous one is kept rather than treated as zero.
    {"event": "screen", "data": {"screen": "no timestamp"}},
    {"event": "read", "ts": 101.5, "data": {"screen": "after a second and a half", "raw_bytes_b64": "aGVsbG8="}},
    # Not wanted by default.
    {"event": "write", "ts": 102.0, "data": {"screen": "a write"}},
    # A read with bytes but no screen: the raw rebuild wants it, the viewer
    # does not.
    {"event": "read", "ts": 102.5, "data": {"raw_bytes_b64": "IHdvcmxk"}},
    # An empty screen is a real frame — a cleared terminal.
    {"event": "screen", "ts": 103.0, "data": {"screen": ""}},
    # No data at all.
    {"event": "screen", "ts": 103.5},
    # Timestamps that go backwards, which a merged or clock-adjusted log has.
    {"event": "screen", "ts": 102.0, "data": {"screen": "backwards"}},
    {"event": "read", "ts": 110.0, "data": {"screen": "much later", "raw_bytes_b64": ""}},
]

# Blank lines, which both readers skip.
BLANK_LINES = ["", "   "]

# (name, line) — lines that are not records. The two readers do not agree on
# these, and the disagreement is worth pinning rather than smoothing over.
HOSTILE_LINES: list[tuple[str, str]] = [
    ("corrupt json", "{not json"),
    # A record whose `data` is not a map. Both readers reach into it.
    ("data is a list", json.dumps({"event": "read", "ts": 1.0, "data": ["not", "a", "map"]})),
    ("data is a string", json.dumps({"event": "screen", "ts": 1.0, "data": "not a map"})),
    # The same malformed data on an event nobody asked for. The viewer filters
    # by event *before* it reaches into data, so this one is harmless.
    ("data is a string on an unwanted event", json.dumps({"event": "write", "ts": 1.0, "data": "not a map"})),
    ("a json null", "null"),
    ("a json array", "[]"),
    ("a json string", '"just a string"'),
]

# (name, kwargs) — the playback settings that change the schedule.
PLAYBACK_CASES: list[tuple[str, dict[str, Any]]] = [
    ("real time", {}),
    ("double speed", {"speed": 2.0}),
    ("half speed", {"speed": 0.5}),
    ("faster than the ceiling", {"speed": 1000.0}),
    ("slower than the floor", {"speed": 0.001}),
    ("zero", {"speed": 0.0}),
    ("negative", {"speed": -1.0}),
    ("stepping", {"step": True}),
    ("only reads", {"events": ["read"]}),
    ("an event that is not in the log", {"events": ["keystroke"]}),
    ("every event named", {"events": ["read", "screen", "write"]}),
]


def _write_log(directory: Path, name: str, records: list[Any], extra: list[str]) -> Path:
    """Write a log, with any extra lines interleaved among the records."""
    lines = [json.dumps(record) for record in records]
    if extra:
        lines = [extra[0], *lines[:2], *extra[1:], *lines[2:]]
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _failure(call: Any) -> str | None:
    """Run `call` and name whatever escapes."""
    try:
        call()
    except Exception as exc:  # recording what escapes is the point
        return type(exc).__name__
    return None


def _record_playback(path: Path, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Replay once, capturing the frames written and the delays slept."""
    slept: list[float] = []
    real_sleep = time.sleep
    real_input = viewer_module.input if hasattr(viewer_module, "input") else input
    prompts: list[str] = []

    time.sleep = lambda seconds: slept.append(seconds)  # type: ignore[assignment]
    viewer_module.input = lambda prompt="": prompts.append(prompt)  # type: ignore[attr-defined]
    try:
        out = io.StringIO()
        viewer_module.replay_log(path, output=out, **kwargs)
        written = out.getvalue()
    finally:
        time.sleep = real_sleep  # type: ignore[assignment]
        if real_input is input:
            viewer_module.input = real_input  # type: ignore[attr-defined]

    return {"output": written, "slept": slept, "prompts": prompts}


def main() -> int:
    """Write the golden corpus and report the case count."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = _write_log(root, "session.jsonl", LOG_RECORDS, BLANK_LINES)

        out_path = root / "raw.bin"
        raw_module.rebuild_raw_stream(path, out_path)
        rebuilt = out_path.read_bytes()

        empty_log = root / "empty.jsonl"
        empty_log.write_text("", encoding="utf-8")
        empty_out = root / "empty.bin"
        raw_module.rebuild_raw_stream(empty_log, empty_out)

        # The two readers disagree about a line that is not a record: the
        # viewer skips corrupt JSON and the raw rebuild does not, and neither
        # survives a line that parses to something other than an object.
        hostile: dict[str, dict[str, str | None]] = {}
        for case, line in HOSTILE_LINES:
            hostile_path = _write_log(root, f"hostile-{case}.jsonl", LOG_RECORDS, [line])
            hostile[case] = {
                "rebuild": _failure(lambda p=hostile_path: raw_module.rebuild_raw_stream(p, root / "h.bin")),
                "replay": _failure(lambda p=hostile_path: viewer_module.replay_log(p, output=io.StringIO())),
            }

        corpus = {
            "hostile_lines": hostile,
            "log_lines": path.read_text(encoding="utf-8").splitlines(),
            "clear_screen": "\x1b[2J\x1b[H",
            "raw_stream": {
                "bytes": list(rebuilt),
                "text": base64.b64encode(rebuilt).decode(),
                "empty_log_bytes": list(empty_out.read_bytes()),
            },
            "playback": [{"name": name, **_record_playback(path, kwargs)} for name, kwargs in PLAYBACK_CASES],
            "default_events": ["read", "screen"],
            "speed_floor": 0.01,
            "speed_ceiling": 100.0,
        }

    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['playback'])} playbacks, {len(corpus['raw_stream']['bytes'])} raw bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
