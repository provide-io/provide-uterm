#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the SSH session connector.

The third of the remote-endpoint connectors, and the one with a real security
decision in its constructor: **host-key verification is on unless it is
explicitly turned off**.

A session with no ``known_hosts`` is refused at construction, and the refusal
names the session and the host and says both ways out of it. The only way to
connect without checking the host key is to say ``insecure_no_host_check``,
which is a word an operator has to write down — and it still logs a warning.
Without that rule, an SSH connector pointed at a name would accept whatever
answered, which is the whole attack host keys exist to stop.

Two smaller decisions are recorded with it: ``client_key_path`` is refused by
name rather than ignored, and the key material a session may carry is a closed
set like every other connector's settings.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_sshconnector_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.server.connectors.ssh import SshSessionConnector

OUT = Path(__file__).resolve().parent / "sshconnector_golden.json"

FIXED_TS = 1_700_000_000.0

# The least a session can say and still be built: a host key file, or the
# explicit words that turn the check off.
CHECKED = {"known_hosts": "/etc/ssh/known_hosts"}
UNCHECKED = {"insecure_no_host_check": True}


class ScriptedStdout:
    """An SSH stdout that answers from a list rather than a shell."""

    def __init__(self, chunks: list[bytes | str]) -> None:
        self._chunks = list(chunks)

    async def read(self, size: int) -> bytes | str:
        if not self._chunks:
            # Nothing to read is a timeout, which is not a disconnection.
            raise TimeoutError
        return self._chunks.pop(0)


class ScriptedStdin:
    """An SSH stdin that records what it was written."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.eof = 0

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None

    def write_eof(self) -> None:
        self.eof += 1


def _fix(message: dict[str, Any]) -> dict[str, Any]:
    fixed = dict(message)
    if "ts" in fixed:
        fixed["ts"] = FIXED_TS
    return fixed


def _connector(config: dict[str, Any]) -> SshSessionConnector:
    return SshSessionConnector("sess-1", "Demo Session", {**CHECKED, **config})


async def _started(config: dict[str, Any], chunks: list[bytes | str]) -> SshSessionConnector:
    connector = _connector(config)
    connector._stdout = ScriptedStdout(chunks)
    connector._stdin = ScriptedStdin()
    connector._conn = object()
    connector._connected = True
    return connector


async def _drive(config: dict[str, Any], chunks: list[bytes | str], steps: list[tuple[str, Any]]) -> dict[str, Any]:
    connector = await _started(config, chunks)
    stdin = connector._stdin
    produced: list[dict[str, Any]] = []
    for action, argument in steps:
        if action == "poll":
            produced.extend(_fix(message) for message in await connector.poll_messages())
        elif action == "input":
            produced.extend(_fix(message) for message in await connector.handle_input(argument))
        elif action == "control":
            produced.extend(_fix(message) for message in await connector.handle_control(argument))
        elif action == "mode":
            produced.extend(_fix(message) for message in await connector.set_mode(argument))
        elif action == "clear":
            produced.extend(_fix(message) for message in await connector.clear())
    return {
        "messages": produced,
        "snapshot": _fix(await connector.get_snapshot()),
        "analysis": await connector.get_analysis(),
        "sent": [chunk.decode("latin-1") for chunk in stdin.written],
        "connected": connector.is_connected(),
    }


CONFIG_CASES: list[tuple[str, dict[str, Any]]] = [
    ("a host key file", {"known_hosts": "/etc/ssh/known_hosts"}),
    ("no host key file at all", {}),
    ("a host key file given null", {"known_hosts": None}),
    ("the host check turned off in so many words", {"insecure_no_host_check": True}),
    ("the host check turned off with a false", {"insecure_no_host_check": False}),
    ("the host check turned off with a string", {"insecure_no_host_check": "yes"}),
    ("the host check turned off with an empty string", {"insecure_no_host_check": ""}),
    ("the host check turned off with an empty list", {"insecure_no_host_check": []}),
    ("the host check turned off with a zero", {"insecure_no_host_check": 0}),
    ("the host check turned off with an empty table", {"insecure_no_host_check": {}}),
    ("the host check turned off with a table", {"insecure_no_host_check": {"why": "testing"}}),
    ("the host check turned off with a one", {"insecure_no_host_check": 1}),
    ("a key file path and no host key file", {"client_key_path": "/x"}),
    ("a name nobody defined and no host key file", {"hostname": "h"}),
    (
        "a host key file and the check turned off",
        {"known_hosts": "/etc/ssh/known_hosts", "insecure_no_host_check": True},
    ),
    ("a host, a port and a user", {"known_hosts": "/k", "host": "shell.example", "port": 2222, "username": "ada"}),
    ("no user named", {"known_hosts": "/k"}),
    ("a password", {"known_hosts": "/k", "password": "hunter2"}),  # pragma: allowlist secret
    ("a password given null", {"known_hosts": "/k", "password": None}),
    ("a key file path", {"known_hosts": "/k", "client_key_path": "/home/ada/.ssh/id_ed25519"}),
    ("a key named", {"known_hosts": "/k", "client_key": "/home/ada/.ssh/id_ed25519"}),
    ("several keys named", {"known_hosts": "/k", "client_keys": ["/a", "/b"]}),
    ("one key named as a list", {"known_hosts": "/k", "client_keys": "/only"}),
    ("a list of keys with a hole in it", {"known_hosts": "/k", "client_keys": ["/a", None, "/b"]}),
    ("no keys at all", {"known_hosts": "/k", "client_keys": None}),
    ("the hijack input mode", {"known_hosts": "/k", "input_mode": "hijack"}),
    ("the overlay turned off", {"known_hosts": "/k", "hub_overlay": False}),
    ("private targets blocked", {"known_hosts": "/k", "block_private_connector_targets": True}),
    ("a setting nobody defined", {"known_hosts": "/k", "hostname": "shell.example"}),
    ("two settings nobody defined", {"known_hosts": "/k", "hostname": "h", "user": "ada"}),
    ("a port that is not whole", {"known_hosts": "/k", "port": "22.5"}),
]

DRIVE_CASES: list[tuple[str, dict[str, Any], list[bytes | str], list[tuple[str, Any]]]] = [
    ("a session that says nothing", {}, [], []),
    ("one chunk of output", {}, [b"ada@shell:~$ \r\n"], [("poll", None)]),
    ("output with high-byte drawing characters", {}, [bytes([0xC9, 0xCD, 0xBB, 0x0A])], [("poll", None)]),
    ("output that arrives as text", {}, ["already decoded\n"], [("poll", None)]),
    ("output as text with a character latin-1 cannot hold", {}, ["snow ☃\n"], [("poll", None)]),
    ("output as text at the last character latin-1 can hold", {}, ["ÿ\n"], [("poll", None)]),
    ("two chunks, counted", {}, [b"first\n", b"second\n"], [("poll", None), ("poll", None)]),
    ("nothing to read", {}, [], [("poll", None)]),
    ("an empty read", {}, [b""], [("poll", None)]),
    ("input sent upstream", {}, [], [("input", "ls -la\r\n")]),
    ("input outside ASCII sent upstream", {}, [], [("input", "echo naïve\r\n")]),
    ("control taken and held", {}, [], [("control", "pause")]),
    ("control taken and released", {}, [], [("control", "pause"), ("control", "resume")]),
    ("a step requested", {}, [], [("control", "step")]),
    ("a control action nobody defined", {}, [], [("control", "rewind")]),
    ("the mode changed to hijack", {}, [], [("mode", "hijack")]),
    ("the mode changed back", {"input_mode": "hijack"}, [], [("mode", "open")]),
    ("the screen cleared", {}, [b"some output\n"], [("poll", None), ("clear", None)]),
    ("the overlay turned off", {"hub_overlay": False}, [b"bare output\n"], [("poll", None)]),
    (
        "more output than the buffer holds",
        {"hub_overlay": False},
        [b"FIRST\n" + b"x" * 33_000 + b"\nLAST\n"],
        [("poll", None)],
    ),
    ("a line wider than the screen", {"hub_overlay": False}, [b"y" * 100 + b"\n"], [("poll", None)]),
    (
        "more lines than the screen has, with no overlay",
        {"hub_overlay": False},
        [("\n".join(f"row {index}" for index in range(30))).encode()],
        [("poll", None)],
    ),
    (
        "more output than the screen holds",
        {},
        [("\n".join(f"line {index}" for index in range(40))).encode()],
        [("poll", None)],
    ),
]


async def _mode_refusal() -> dict[str, Any]:
    connector = _connector({})
    try:
        await connector.set_mode("readonly")
    except ValueError as exc:
        return {"error": str(exc)}
    return {"error": None}


async def _stop_case() -> dict[str, Any]:
    """Stopping tears down the shell, the process and the connection, in order."""

    class ScriptedProcess:
        def __init__(self) -> None:
            self.closes = 0

        def close(self) -> None:
            self.closes += 1

    class ScriptedConn:
        def __init__(self) -> None:
            self.closes = 0
            self.waits = 0

        def close(self) -> None:
            self.closes += 1

        async def wait_closed(self) -> None:
            self.waits += 1

    connector = await _started({}, [])
    process = ScriptedProcess()
    conn = ScriptedConn()
    connector._process = process
    connector._conn = conn
    stdin = connector._stdin
    await connector.stop()
    return {
        "eof": stdin.eof,
        "process_closes": process.closes,
        "conn_closes": conn.closes,
        "conn_waits": conn.waits,
        "connected": connector.is_connected(),
    }


async def _partial_states() -> dict[str, Any]:
    """What counts as connected: everything the connector holds, or nothing."""
    states = {}
    for name, drop in (
        ("everything held", None),
        ("no output stream", "_stdout"),
        ("no input stream", "_stdin"),
        ("no connection", "_conn"),
        ("the flag cleared", "_connected"),
    ):
        connector = await _started({}, [])
        if drop == "_connected":
            connector._connected = False
        elif drop is not None:
            setattr(connector, drop, None)
        states[name] = {"connected": connector.is_connected(), "polls": len(await connector.poll_messages())}
    return states


async def main_async() -> None:
    config_outcomes = []
    for name, config in CONFIG_CASES:
        try:
            connector = SshSessionConnector("sess-1", "Demo Session", config)
        except (TypeError, ValueError) as exc:
            config_outcomes.append({"name": name, "config": config, "error": str(exc)})
            continue
        config_outcomes.append(
            {
                "name": name,
                "config": config,
                "error": None,
                "snapshot": _fix(await connector.get_snapshot()),
                "analysis": await connector.get_analysis(),
                "client_keys": [str(key) for key in connector._client_keys],
            }
        )

    corpus = {
        "fixed_ts": FIXED_TS,
        "cols": 80,
        "rows": 25,
        "config_cases": config_outcomes,
        "drive_cases": [
            {
                "name": name,
                "config": config,
                "chunks": [
                    {"binary": True, "data": chunk.decode("latin-1")}
                    if isinstance(chunk, bytes)
                    else {"binary": False, "data": chunk}
                    for chunk in chunks
                ],
                **await _drive(config, chunks, steps),
            }
            for name, config, chunks, steps in DRIVE_CASES
        ],
        "mode_refusal": await _mode_refusal(),
        "stop": await _stop_case(),
        "partial_states": await _partial_states(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['drive_cases'])} driven cases)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
