#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript connectors port.

Two things are recorded here.

**The registry.** It maps a `connector_type` string to a factory, and an
unknown type has to be a refusal rather than a default — a session created
with a typo must not silently land on some other transport.

**The reference shell connector.** It is the thing every other port is checked
against by hand, and it is the one connector with no network underneath it, so
it is the honest place to pin the *worker protocol* itself: what a snapshot
carries, what a mode change emits, and what happens on input nobody planned
for. Its config accepts exactly one key, and an unknown key is refused — a
typo in a session's config would otherwise be accepted and silently ignored.

Every timestamp is replaced with a counter, so the corpus is about the
protocol rather than about when it was recorded.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_connectors_golden.py
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from provide.uterm.server.connectors import registry as registry_module
from provide.uterm.server.connectors.shell import ShellSessionConnector

OUT = Path(__file__).with_name("connectors_golden.json")

SESSION_ID = "w1"
DISPLAY_NAME = "Reference session"

# The inputs a session actually receives, in order. Each one is recorded with
# the messages it produced.
SCRIPT: list[tuple[str, str, str]] = [
    ("plain text", "input", "hello there"),
    ("text needing normalisation", "input", "  spaced\tout\r"),
    ("empty input", "input", "   "),
    ("help", "input", "/help"),
    ("status", "input", "/status"),
    ("nick", "input", "/nick alice"),
    ("nick with no argument", "input", "/nick"),
    ("nick that is too long", "input", "/nick " + "n" * 40),
    # Extra spaces after the command: the argument is trimmed, so the
    # nickname is not stored with them.
    ("nick with extra spaces", "input", "/nick    bob"),
    ("say", "input", "/say something"),
    ("say with no argument", "input", "/say"),
    ("shell", "input", "/shell"),
    ("mode hijack", "input", "/mode hijack"),
    ("mode with a bad argument", "input", "/mode sideways"),
    ("mode open", "input", "/mode OPEN"),
    ("an unknown command", "input", "/wat"),
    ("clear", "input", "/clear"),
    ("control pause", "control", "pause"),
    ("control step", "control", "step"),
    ("control resume", "control", "resume"),
    ("an unknown control action", "control", "explode"),
    ("reset", "input", "/reset"),
]


def _stable(value: Any) -> Any:
    """Replace every timestamp so the corpus is about the protocol."""
    if isinstance(value, dict):
        return {key: ("<ts>" if key == "ts" else _stable(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _failure(call: Any) -> str | None:
    """Run `call` and return the refusal, or None when it is accepted."""
    try:
        call()
    except ValueError as exc:
        return str(exc)
    return None


async def _record_script() -> list[dict[str, Any]]:
    """Drive the connector through the script, recording every message."""
    connector = ShellSessionConnector(SESSION_ID, DISPLAY_NAME, {})
    await connector.start()
    steps = []
    for name, kind, payload in SCRIPT:
        messages = await connector.handle_input(payload) if kind == "input" else await connector.handle_control(payload)
        steps.append(
            {
                "name": name,
                "kind": kind,
                "payload": payload,
                "messages": _stable(messages),
                "analysis": await connector.get_analysis(),
                "connected": connector.is_connected(),
            }
        )
    return steps


async def _main() -> int:
    """Write the golden corpus and report the case count."""
    counter = {"now": 1000.0}
    real_time = time.time

    def fake_time() -> float:
        counter["now"] += 1.0
        return counter["now"]

    time.time = fake_time  # type: ignore[assignment]
    try:
        steps = await _record_script()

        fresh = ShellSessionConnector(SESSION_ID, DISPLAY_NAME, {})
        lifecycle = {
            "connected_before_start": fresh.is_connected(),
        }
        await fresh.start()
        lifecycle["connected_after_start"] = fresh.is_connected()
        await fresh.stop()
        lifecycle["connected_after_stop"] = fresh.is_connected()
        lifecycle["poll_is_empty"] = await fresh.poll_messages() == []
        lifecycle["initial_snapshot"] = _stable(await fresh.get_snapshot())
        lifecycle["initial_analysis"] = await fresh.get_analysis()
        lifecycle["cleared"] = _stable(await fresh.clear())
        lifecycle["set_mode_hijack"] = _stable(await fresh.set_mode("hijack"))

        corpus = {
            "session_id": SESSION_ID,
            "display_name": DISPLAY_NAME,
            "script": steps,
            "lifecycle": lifecycle,
            "config": {
                "valid_keys": ["input_mode"],
                "unknown_key": _failure(lambda: ShellSessionConnector(SESSION_ID, DISPLAY_NAME, {"host": "x"})),
                "several_unknown_keys": _failure(
                    lambda: ShellSessionConnector(SESSION_ID, DISPLAY_NAME, {"zeta": 1, "alpha": 2})
                ),
                "no_config_at_all": _failure(lambda: ShellSessionConnector(SESSION_ID, DISPLAY_NAME, None)),
                "input_mode_hijack": ShellSessionConnector(
                    SESSION_ID, DISPLAY_NAME, {"input_mode": "hijack"}
                )._input_mode,
                "input_mode_default": ShellSessionConnector(SESSION_ID, DISPLAY_NAME, {})._input_mode,
            },
            "registry": {
                "builtin_types": sorted(registry_module._BUILTIN_CLASSES),
                "unknown_type": _failure(
                    lambda: registry_module.build_connector(SESSION_ID, DISPLAY_NAME, "carrier-pigeon", {})
                ),
                "shell_builds": type(registry_module.build_connector(SESSION_ID, DISPLAY_NAME, "shell", {})).__name__,
            },
            "transcript_limit": 10,
            "cols": 80,
            "rows": 25,
        }
    finally:
        time.time = real_time  # type: ignore[assignment]

    # The invalid-mode refusal, recorded outside the patched clock.
    connector = ShellSessionConnector(SESSION_ID, DISPLAY_NAME, {})
    try:
        await connector.set_mode("sideways")
    except ValueError as exc:
        corpus["lifecycle"]["set_mode_invalid"] = str(exc)

    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(steps)} script steps)")
    return 0


def main() -> int:
    """Entry point."""
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
