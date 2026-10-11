#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript server's session recording.

The reference records a hosted session from inside ``HostedSessionRuntime``:
every frame the worker sends goes through ``_send_outbound_frame`` (raw wire
and control entries in ``wire`` mode, a ``read`` entry for a snapshot), and
every frame it receives through the inbound path (``wire_recv``,
``control_recv``, and a ``send`` entry for typed input — masked while the last
screen ended at a password prompt). This drives a real runtime's own methods
through one fixed script under each recording configuration that changes what
is written, and records every entry the store received.

The script is written into the corpus, so the port replays exactly these
steps rather than a restatement of them.

A second script runs with the reference's ``PatternDetector`` attached, which
is how the server factory builds every runtime: each match is an
``annotation`` entry. Read-path rules run on each snapshot's screen, on each
streamed ``term`` frame with escape sequences removed (carrying a partial
match, and an unterminated escape sequence, across frames), and send-path
rules on typed input (carrying likewise). The snapshot path skips a read-path
match this recording already holds, from the stream or an earlier snapshot. Every ``ts`` in the script has a
fractional part: an integral float is ``4.0`` on CPython's wire and ``4`` on
JavaScript's, a difference in number models rather than in recording, and a
connector's stamps are never integral anyway. Wall-clock ``ts`` fields and the
``started_at`` stamp are stripped; they are fresh by design.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_session_recording_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from provide.uterm.annotation import PatternDetector

from provide.uterm.recording import InMemoryRecordingStore
from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime

OUT = Path(__file__).with_name("session_recording_golden.json")

SESSION_ID = "rec"

# One step per entry: the kind of traffic, and what it carried.
SCRIPT: list[list[Any]] = [
    ["event", "runtime_started", {"session_id": SESSION_ID}],
    ["outbound", {"type": "snapshot", "screen": "$ ", "cursor": {"x": 2, "y": 0}, "ts": 1.5}],
    ["wire_recv", "ls\r"],
    ["send", "ls\r"],
    ["outbound", {"type": "term", "data": "\x1b[1mfile\x1b[0m\r\n", "ts": 2.25}],
    # A prompt with trailing blanks still counts: the screen is right-stripped.
    ["outbound", {"type": "snapshot", "screen": "login: tim\nPassword: ", "ts": 3.5}],
    ["send", "hunter2\r"],
    ["outbound", {"type": "snapshot", "screen": "Enter PASSPHRASE for key:\n\n", "ts": 4.75}],
    # Masked by length in cp437, where an accented letter is one byte.
    ["send", "s3crét"],
    # A rendered prompt row ends in a reset: the escape code is stripped before
    # the prompt check, so the typed password is still masked.
    ["outbound", {"type": "snapshot", "screen": "\x1b[1msudo\x1b[0m password for tim: \x1b[0m", "ts": 5.25}],
    ["send", "pw\r"],
    # Not a prompt: the colon is not at the end of what is on screen.
    ["outbound", {"type": "snapshot", "screen": "$ echo password=hunter2 done\n$ ", "ts": 5.5}],
    ["send", "export AWS=AKIAIOSFODNN7EXAMPLE\r"],
    # A character cp437 cannot carry is recorded as its replacement byte.
    ["send", "snow ☃\r"],
    ["control_recv", {"type": "snapshot_req"}],
    ["outbound", {"type": "hijack_state", "enabled": True, "owner": "ops"}],
]

_KEY = "AKIA0123456789AB"  # pragma: allowlist secret

# Every path a rule can match on, and the ones it must not.
ANNOTATED_SCRIPT: list[list[Any]] = [
    ["event", "runtime_started", {"session_id": SESSION_ID}],
    ["outbound", {"type": "snapshot", "screen": "$ ", "ts": 1.5}],
    # Typed input split mid-word still matches once, on the chunk completing it.
    ["send", "sud"],
    ["send", "o -i\r"],
    # Styled streamed output is scanned through its escape sequences.
    ["outbound", {"type": "term", "data": f"\x1b[12;5H\x1b[38;2;255;176;0m{_KEY}\x1b[0m", "ts": 2.25}],
    # And a match split across frames is found once.
    ["outbound", {"type": "term", "data": "DROP TA", "ts": 2.5}],
    ["outbound", {"type": "term", "data": "BLE callers;", "ts": 2.75}],
    ["outbound", {"type": "term", "data": "", "ts": 2.8}],
    # A whole screen is scanned on its own, several categories at once.
    ["outbound", {"type": "snapshot", "screen": f"# rm -rf /tmp/x\n# echo {_KEY}\n# ", "ts": 3.5}],
    # The same screen again records no annotation twice: the snapshot path
    # skips a match an earlier snapshot (or the stream) already recorded.
    ["outbound", {"type": "snapshot", "screen": f"# rm -rf /tmp/x\n# echo {_KEY}\n# ", "ts": 3.75}],
    # An escape sequence split across frames is carried and stripped whole, so
    # the styled word after it still matches; the stream records it ...
    ["outbound", {"type": "term", "data": "$ \x1b[1", "ts": 4.0}],
    ["outbound", {"type": "term", "data": "msudo\x1b[0m rm x\r\n", "ts": 4.1}],
    # ... and the snapshot of the same (styled) text skips it as already seen.
    ["outbound", {"type": "snapshot", "screen": "$ \x1b[1msudo\x1b[0m rm x\n$ ", "ts": 4.2}],
    # A run of escape-looking bytes past the carry bound is released, not held.
    ["outbound", {"type": "term", "data": "\x1b[" + "1;" * 40, "ts": 4.3}],
    ["outbound", {"type": "term", "data": "mDROP TABLE t;", "ts": 4.4}],
    # A trailing reset does not hide the prompt from the check.
    ["outbound", {"type": "snapshot", "screen": "Password: \x1b[0m", "ts": 4.5}],
    # Masked in the recording, and still annotated: the reference scans the
    # input itself, not what it wrote.
    ["send", "shutdown now\r"],
    ["outbound", {"type": "hijack_state", "enabled": True, "owner": "ops"}],
]

CONFIGS: dict[str, dict[str, Any]] = {
    "exclude": {},
    "wire": {"control_channel_mode": "wire"},
    "unredacted": {"redact_sensitive": False},
}


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


async def _drive(
    overrides: dict[str, Any], script: list[list[Any]] = SCRIPT, *, annotated: bool = False
) -> list[dict[str, Any]]:
    """Run a script through one runtime and return what was recorded."""
    store = InMemoryRecordingStore()
    runtime = HostedSessionRuntime(
        SessionDefinition(session_id=SESSION_ID, display_name="Rec", connector_type="shell", auto_start=False),
        public_base_url="http://127.0.0.1:1",
        recording=RecordingConfig.model_validate({"enabled_by_default": True, "flush_interval_s": 3600, **overrides}),
        recording_store=store,
        detector=PatternDetector() if annotated else None,
    )
    await runtime._start_recording()
    ws = AsyncMock()
    for kind, *args in script:
        if kind == "outbound":
            await runtime._send_outbound_frame(ws, args[0])
        elif kind == "send":
            await runtime._log_send(args[0])
        elif kind == "wire_recv":
            await runtime._log_wire_recv(args[0])
        elif kind == "control_recv":
            await runtime._log_control_recv(args[0])
        else:
            await runtime._log_event(args[0], args[1])
    await runtime._stop_recording()
    return _strip(await store.get_entries(SESSION_ID, limit=500))


async def _run() -> dict[str, Any]:
    """Build every section of the corpus."""
    recorded = {name: await _drive(overrides) for name, overrides in CONFIGS.items()}
    recorded["annotated"] = await _drive({}, ANNOTATED_SCRIPT, annotated=True)
    return recorded


def main() -> int:
    """Write the golden corpus and report the record count."""
    recorded = asyncio.run(_run())
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_session_recording_golden.py",
        "session_id": SESSION_ID,
        "script": SCRIPT,
        "annotated_script": ANNOTATED_SCRIPT,
        "configs": CONFIGS,
        "recorded": recorded,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(recorded['exclude'])} exclude-mode entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
