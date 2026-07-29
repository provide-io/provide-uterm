#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the PTY connector's state machine.

The connector is the thing that actually runs a shell, so it is mostly
operating-system calls — but the decisions *around* those calls are where a
session goes wrong, and none of them needs a real terminal to check:

* **Nothing is stored before everything is validated.** An unknown config key,
  a missing command, a bad username, an environment key with an `=` in it or
  an input mode nobody defined all refuse construction, so a half-configured
  connector never exists.
* **Two pauses that must not interfere.** A hijack pause stops output *and*
  input; backpressure stops only reading, and says nothing while it does —
  a snapshot during congestion is traffic added to the congestion it is
  relieving.
* **The buffer is capped from the end**, so a session that has scrolled for a
  week still costs the same and still shows the newest output.
* **Decoding is incremental.** A 4096-byte read can split a multibyte
  character; decoding each read on its own would turn both halves into
  replacement characters and corrupt it permanently. `clear` deliberately
  does not reset the decoder, so a character straddling a clear still
  completes.
* **The end of the child is noticed both ways it arrives**: an error on Linux,
  an empty read on macOS.

The connector is driven with the fork replaced — the master descriptor and
child pid are set as a started connector would have them, and reads come from
a script — so every recorded value is one the real code produced.

# uv-package: provide-uterm-platform

Usage (from the repository root)::

    uv run --package provide-uterm-platform python \\
        packages/provide-uterm-ts/testdata/gen_ptyconnector_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.pty.connector import PTYConnector

OUT = Path(__file__).resolve().parent / "ptyconnector_golden.json"

CONFIGS: list[tuple[str, dict[str, Any]]] = [
    ("a shell", {"command": "/bin/sh"}),
    ("a command with arguments", {"command": "/bin/sh", "args": ["-c", "echo hi"]}),
    ("no command at all", {}),
    ("a command that is empty", {"command": ""}),
    ("a command that is not absolute", {"command": "sh"}),
    ("a command holding a null byte", {"command": "/bin/sh\x00"}),
    ("a key nobody defined", {"command": "/bin/sh", "colour": "green"}),
    ("a size given", {"command": "/bin/sh", "cols": 120, "rows": 40}),
    ("a size given as text", {"command": "/bin/sh", "cols": "120", "rows": "40"}),
    ("an input mode", {"command": "/bin/sh", "input_mode": "hijack"}),
    ("an input mode nobody defined", {"command": "/bin/sh", "input_mode": "sideways"}),
    ("an environment", {"command": "/bin/sh", "env": {"TERM": "xterm"}}),
    ("an environment key with an equals in it", {"command": "/bin/sh", "env": {"A=B": "c"}}),
    ("a username", {"command": "/bin/sh", "username": "ada"}),
    ("a username with a slash in it", {"command": "/bin/sh", "username": "a/b"}),
    ("injection asked for", {"command": "/bin/sh", "inject": True}),
    # The reference takes this by truth, not by identity: a config written by
    # hand or read from TOML may say 1, or "yes".
    ("injection asked for with a number", {"command": "/bin/sh", "inject": 1}),
    ("injection asked for with text", {"command": "/bin/sh", "inject": "yes"}),
    ("injection turned off with a zero", {"command": "/bin/sh", "inject": 0}),
    ("injection turned off with empty text", {"command": "/bin/sh", "inject": ""}),
    ("a user to run as", {"command": "/bin/sh", "run_as": "ada", "run_as_uid": 1000, "run_as_gid": 1000}),
]


def _construct(name: str, config: dict[str, Any]) -> dict[str, Any]:
    """Build a connector, recording the refusal if there is one."""
    try:
        connector = PTYConnector("sess-1", "a session", dict(config))
    except Exception as exc:
        return {"name": name, "config": config, "error": type(exc).__name__, "message": str(exc)}
    return {
        "name": name,
        "config": config,
        "error": None,
        "state": {
            "cols": connector._cols,
            "rows": connector._rows,
            "input_mode": connector._input_mode,
            "args": connector._args,
            "inject": connector._inject,
            "connected": connector.is_connected(),
        },
    }


class ScriptedReads:
    """Stands in for the master descriptor, handing over a script of reads."""

    def __init__(self, chunks: list[str | None]) -> None:
        # A string is bytes to hand over (latin-1), None is the child going away.
        self._chunks = list(chunks)

    def __call__(self) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if chunk is None:
            return b""
        return chunk.encode("latin-1")


def _started(config: dict[str, Any], chunks: list[str | None]) -> PTYConnector:
    """A connector in the state `start` would have left it, without forking."""
    connector = PTYConnector("sess-1", "a session", dict(config))
    connector._master_fd = 99
    connector._child_pid = 12345
    connector._connected = True
    reads = ScriptedReads(chunks)

    def read_master() -> bytes:
        data = reads()
        if not data:
            # What the real reader does at end of file: the child is gone.
            connector._connected = False
        return data

    connector._read_master = read_master  # type: ignore[method-assign]
    connector._write_calls = []  # type: ignore[attr-defined]

    return connector


def _strip(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the wall-clock stamp, which no corpus can hold."""
    return [{key: value for key, value in message.items() if key != "ts"} for message in messages]


async def _drive(name: str, config: dict[str, Any], chunks: list[str | None], steps: list[Any]) -> dict[str, Any]:
    """Run a script of operations against a started connector."""
    connector = _started(config, chunks)
    log: list[dict[str, Any]] = []
    for step in steps:
        action = step[0]
        if action == "poll":
            result = await connector.poll_messages()
        elif action == "control":
            result = await connector.handle_control(step[1])
        elif action == "mode":
            try:
                result = await connector.set_mode(step[1])
            except ValueError as exc:
                log.append({"step": list(step), "error": str(exc)})
                continue
        elif action == "clear":
            result = await connector.clear()
        elif action == "snapshot":
            result = [await connector.get_snapshot()]
        elif action == "analysis":
            log.append({"step": list(step), "analysis": await connector.get_analysis()})
            continue
        else:
            raise AssertionError(f"unknown step {action}")
        log.append(
            {
                "step": list(step),
                "messages": _strip(result),
                "connected": connector.is_connected(),
                "buffer_len": len(connector._buffer),
            }
        )
    return {"name": name, "config": config, "chunks": chunks, "steps": [list(step) for step in steps], "log": log}


BASE = {"command": "/bin/sh"}

SESSIONS: list[tuple[str, dict[str, Any], list[str | None], list[Any]]] = [
    ("output arriving", BASE, ["hello"], [["poll"]]),
    ("nothing to read", BASE, [""], [["poll"]]),
    ("output arriving twice", BASE, ["one", "two"], [["poll"], ["poll"]]),
    ("the child going away", BASE, [None], [["poll"], ["poll"]]),
    (
        "a hijack pause stopping output",
        BASE,
        ["hello", "more"],
        [["poll"], ["control", "pause"], ["poll"], ["control", "resume"], ["poll"]],
    ),
    (
        "a step resuming like a resume",
        BASE,
        ["hello"],
        [["control", "pause"], ["control", "step"], ["poll"]],
    ),
    (
        "backpressure stopping reading and saying nothing",
        BASE,
        ["hello", "more"],
        [["control", "flow_pause"], ["poll"], ["control", "flow_resume"], ["poll"]],
    ),
    (
        "backpressure and a hijack pause together",
        BASE,
        ["hello"],
        [
            ["control", "flow_pause"],
            ["control", "pause"],
            ["control", "flow_resume"],
            ["poll"],
            ["control", "resume"],
            ["poll"],
        ],
    ),
    ("an action nobody defined", BASE, [""], [["control", "sideways"]]),
    ("clearing what was shown", BASE, ["hello"], [["poll"], ["clear"], ["poll"]]),
    ("asking for a snapshot", BASE, ["hello"], [["poll"], ["snapshot"]]),
    ("asking what is going on", BASE, ["hello"], [["poll"], ["analysis"]]),
    ("changing the input mode", BASE, [""], [["mode", "hijack"], ["mode", "open"]]),
    ("a mode nobody defined", BASE, [""], [["mode", "sideways"]]),
    (
        "a character split across two reads",
        BASE,
        # "é" is two bytes; the read boundary falls between them.
        ["\xc3", "\xa9", ""],
        [["poll"], ["poll"], ["poll"]],
    ),
    (
        "a character split across a clear",
        BASE,
        ["\xc3", "\xa9"],
        [["poll"], ["clear"], ["poll"]],
    ),
    ("bytes that are not a character at all", BASE, ["\xff\xfe"], [["poll"]]),
    ("more than the buffer holds", BASE, ["x" * 40000], [["poll"]]),
    (
        "more than the buffer holds, in pieces",
        BASE,
        ["x" * 20000, "y" * 20000],
        [["poll"], ["poll"]],
    ),
    ("a size other than the default", {**BASE, "cols": 120, "rows": 40}, ["hi"], [["poll"]]),
]


async def _input_cases() -> list[dict[str, Any]]:
    """What writing to the child does, including when the child has gone."""
    cases: list[dict[str, Any]] = []
    for name, paused, writes_fail, data in (
        ("ordinary input", False, False, "ls\n"),
        ("input while paused", True, False, "ls\n"),
        ("input after the child has gone", False, True, "ls\n"),
        ("input outside ASCII", False, False, "héllo ☃"),
        ("nothing at all", False, False, ""),
    ):
        connector = _started(BASE, [""])
        connector._paused = paused
        written: list[str] = []

        import os as _os

        real_write = _os.write

        def fake_write(fd: int, payload: bytes, *, _written: list[str] = written, _fails: bool = writes_fail) -> int:
            if _fails:
                raise OSError(5, "input/output error")
            _written.append(payload.decode("utf-8", errors="replace"))
            return len(payload)

        _os.write = fake_write  # type: ignore[assignment]
        try:
            messages = await connector.handle_input(data)
        finally:
            _os.write = real_write  # type: ignore[assignment]

        cases.append(
            {
                "name": name,
                "paused": paused,
                "writes_fail": writes_fail,
                "data": data,
                "written": written,
                "messages": _strip(messages),
                "connected": connector.is_connected(),
            }
        )
    return cases


async def main_async() -> None:
    corpus = {
        "configs": [_construct(name, config) for name, config in CONFIGS],
        "sessions": [await _drive(*case) for case in SESSIONS],
        "inputs": await _input_cases(),
        "buffer_cap": 32768,
        "read_size": 4096,
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['sessions'])} sessions)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
