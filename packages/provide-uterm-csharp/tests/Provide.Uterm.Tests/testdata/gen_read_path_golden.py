#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the read-path annotation golden corpus for the C# ``SessionAnnotator``.

Read-path detector rules run over every streamed ``term`` chunk and every
snapshot screen of a recorded hosted session. The reference
(``provide.uterm.server.runtime.HostedSessionRuntime``) strips escape sequences
from both, carries an escape sequence cut off at a chunk boundary into the next
chunk (``_split_incomplete_escape``), records every stream match and skips any
snapshot match already recorded (``_read_annotation_key``). This corpus pins:

* ``split``: ``_split_incomplete_escape`` on edge inputs, and its constants;
* ``scenarios``: a sequence of outbound ``term``/``snapshot`` frames through a
  real runtime (logger mocked) and the annotation descriptions it records, in
  order, plus the escape carry left at the end.

Usage (from the repository root)::

    uv run python packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/testdata/gen_read_path_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

from provide.uterm.annotation import PatternDetector

from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime
from provide.uterm.server.runtime_helpers import (
    _MAX_ESCAPE_CARRY,
    _MAX_READ_ANNOTATION_KEYS,
    _split_incomplete_escape,
)

OUT = Path(__file__).with_name("read_path_golden.json")

SPLIT_INPUTS: list[str] = [
    "",
    "plain",
    "a\x1b",
    "a\x1b[",
    "a\x1b[38;2;255",
    "a\x1b[1 ",
    "a\x1b[1mb",
    "a\x1b[1m",
    "a\x1bc",
    "a\x1b]0;title",
    "\x1b[1m\x1b[3",
    "\x1b[?25",
    "\x1b[" + "1" * (_MAX_ESCAPE_CARRY - 2),
    "\x1b[" + "1" * (_MAX_ESCAPE_CARRY - 1),
    "x" * 100 + "\x1b[" + "1" * (_MAX_ESCAPE_CARRY - 2),
    "\x1b[" + "1;" * _MAX_ESCAPE_CARRY,
]

# Each step is [kind, text]; kind is "term" or "snapshot".
SCENARIOS: dict[str, list[list[str]]] = {
    "escape_split_across_chunks": [["term", "$ \x1b[1"], ["term", "msudo rm -rf /\r\n"]],
    "bare_esc_carried": [["term", "$ \x1b"], ["term", "[1msudo ls\r\n"]],
    "carry_left_at_end": [["term", "$ sudo ls\r\n\x1b[1"]],
    "overlong_sequence_released": [["term", "\x1b[" + "1;" * _MAX_ESCAPE_CARRY]],
    "snapshot_after_stream_deduped": [["term", "$ sudo ls\r\n"], ["snapshot", "$ sudo ls\n"]],
    "identical_snapshots_once": [
        ["snapshot", "$ rm -rf build\n"],
        ["snapshot", "$ rm -rf build\n"],
        ["snapshot", "$ rm -rf build\n"],
    ],
    "stream_twice_annotated_twice": [
        ["term", "$ sudo ls\r\n"],
        ["term", "$ sudo ls\r\n"],
        ["snapshot", "$ sudo ls\n$ sudo ls\n"],
    ],
    "snapshot_records_what_stream_missed": [
        ["term", "$ sudo ls\r\n"],
        ["snapshot", "$ sudo ls\n$ rm -rf build\n"],
    ],
    "styled_snapshot_is_stripped": [
        ["snapshot", "\x1b[1m$ \x1b[31msu\x1b[0mdo\x1b[0m ls\x1b[0m\n"],
    ],
    "different_matches_same_rule_not_deduped": [
        ["term", "$ ssh alice@alpha\r\n"],
        ["snapshot", "$ ssh bob@bravo\n"],
    ],
}


async def _scenario(steps: list[list[str]]) -> dict[str, object]:
    runtime = HostedSessionRuntime(
        SessionDefinition(session_id="golden", display_name="Golden", connector_type="shell", auto_start=False),
        public_base_url="http://localhost:9999",
        recording=RecordingConfig(),
        detector=PatternDetector(),
    )
    logger = AsyncMock()
    runtime._logger = logger
    for kind, text in steps:
        frame = {"type": kind, "data": text} if kind == "term" else {"type": kind, "screen": text}
        await runtime._send_outbound_frame(AsyncMock(), frame)
    recorded = [call.args[1] for call in logger.log_event.await_args_list if call.args[0] == "annotation"]
    return {
        "steps": steps,
        "annotations": [{"label": a["label"], "description": a["description"]} for a in recorded],
        "carry": runtime._escape_carry,
    }


async def _scenarios() -> dict[str, dict[str, object]]:
    return {name: await _scenario(steps) for name, steps in SCENARIOS.items()}


def main() -> int:
    """Write the golden corpus and report the record count."""
    payload = {
        "generator": "packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/testdata/gen_read_path_golden.py",
        "max_escape_carry": _MAX_ESCAPE_CARRY,
        "max_read_annotation_keys": _MAX_READ_ANNOTATION_KEYS,
        "split": [
            {"input": text, "complete": complete, "carry": carry}
            for text in SPLIT_INPUTS
            for complete, carry in [_split_incomplete_escape(text)]
        ],
        "scenarios": asyncio.run(_scenarios()),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(payload['split'])} split cases, {len(SCENARIOS)} scenarios)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
