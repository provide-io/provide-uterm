#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the shell's command dispatcher.

The dispatcher is what a line typed at `ushell` becomes. Only the routing is
recorded here — where a line goes and what a line that goes nowhere is told —
because that is the part that is the same on every runtime.

* **A command is the first word, lowercased**, and everything after it is one
  argument rather than a list, so a command taking a sentence gets the
  sentence.
* **An empty line is a prompt, not an error.** So is a bare interrupt, which
  has already been echoed.
* **A line that names nothing says so**, and says where to look — an
  unrecognised command silently doing nothing is the worst answer of the
  three.
* **`sessions kill` is a subcommand, not a command**, and `kill` on its own
  is the same subcommand with nothing to kill.

The commands that reach out — a fetch, a key-value store, a Durable Object —
are driven with those replaced, so what is recorded is the routing rather
than somebody's network.

# uv-package: provide-uterm

Usage (from the repository root)::

    uv run --package provide-uterm python \\
        packages/provide-uterm-ts/testdata/gen_dispatcher_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.shell._output import PROMPT
from provide.uterm.shell.commands import dispatcher as dispatcher_module
from provide.uterm.shell.commands.help import _COMMAND_HELP, _HELP

OUT = Path(__file__).resolve().parent / "dispatcher_golden.json"

# Every line whose answer does not depend on anything outside the dispatcher.
LINES: list[tuple[str, str]] = [
    ("nothing at all", ""),
    ("only spaces", "   "),
    ("an interrupt", "\x03"),
    ("leaving", "exit"),
    ("leaving the other way", "quit"),
    ("an end-of-file", "\x04"),
    ("leaving, shouted", "EXIT"),
    ("leaving with something after it", "exit now"),
    ("asking for help", "help"),
    ("asking about a command", "help render"),
    ("asking about a command, shouted", "help RENDER"),
    ("asking about something that is not a command", "help sideways"),
    ("clearing the screen", "clear"),
    ("clearing with something after it", "clear all"),
    ("a command nobody defined", "sideways"),
    ("a command nobody defined, with an argument", "sideways and then some"),
    ("a command with leading spaces", "   help   "),
    ("a command in mixed case", "HeLp"),
]


class FakeEnv:
    """Stands in for the Worker's bindings."""

    SESSIONS = object()
    KV = object()
    _private = object()


async def _run(name: str, line: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Send one line through the real dispatcher."""
    dispatcher = dispatcher_module.CommandDispatcher(ctx)
    output = await dispatcher.dispatch(line)
    return {"name": name, "line": line, "output": output if isinstance(output, list) else None}


async def _env_cases() -> list[dict[str, Any]]:
    """What `env` shows, which depends on what the shell was handed."""
    cases: list[dict[str, Any]] = []
    for name, ctx in (
        ("bindings to show", {"env": FakeEnv()}),
        ("no bindings, but a context", {"storage": object(), "list_kv_sessions": object()}),
        ("nothing at all", {}),
        ("a context with a private key", {"_hidden": 1, "shown": 2}),
    ):
        dispatcher = dispatcher_module.CommandDispatcher(dict(ctx))
        cases.append({"name": name, "keys": sorted(str(k) for k in ctx), "output": await dispatcher.dispatch("env")})
    return cases


async def main_async() -> None:
    corpus = {
        "prompt": PROMPT,
        "help": _HELP,
        "command_help": dict(sorted(_COMMAND_HELP.items())),
        "lines": [await _run(name, line, {}) for name, line in LINES],
        "env": await _env_cases(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['lines'])} lines)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
