#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the telnet session connector.

A hosted session backed by a telnet endpoint. What is recorded here is
everything a viewer would see and everything the connector refuses:

* **The settings it will take.** A closed set, so a mistyped key is a refusal
  rather than a setting that silently does nothing — the opposite of the
  ``[[sessions]]`` fold, and the reason that fold is worth knowing about: a
  key the entry folds away is a key this connector then rejects by name.
* **The overlay a viewer reads.** Who is connected, to what, in which input
  mode, and whether control is held. It is rebuilt on every message, so its
  wording is part of the wire.
* **The snapshot.** Cursor, size, and a hash of the screen, which is what a
  browser reconciles against.

The connector is driven with a transport that answers from a script, so the
recorded stream is the connector's own behaviour rather than a network's.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_telnetconnector_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.server.connectors.telnet import TelnetSessionConnector

OUT = Path(__file__).resolve().parent / "telnetconnector_golden.json"

# Every recorded timestamp is replaced with this, so the corpus does not differ
# from itself on every run. What matters is that the field is there and what
# the rest of the message says.
FIXED_TS = 1_700_000_000.0


class ScriptedTransport:
    """A telnet transport that answers from a list rather than a socket."""

    def __init__(self, chunks: list[bytes], peer_ip: str | None = "203.0.113.7") -> None:
        self._chunks = list(chunks)
        self._peer_ip = peer_ip
        self.sent: list[bytes] = []
        self.connected = False
        self.disconnects = 0

    async def connect(self, host: str, port: int) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnects += 1

    def is_connected(self) -> bool:
        return self.connected

    def peer_ip(self) -> str | None:
        return self._peer_ip

    async def receive(self, size: int, timeout_ms: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    async def send(self, data: bytes) -> None:
        self.sent.append(data)


def _fix(message: dict[str, Any]) -> dict[str, Any]:
    """Replace the wall clock so the corpus is reproducible."""
    fixed = dict(message)
    if "ts" in fixed:
        fixed["ts"] = FIXED_TS
    return fixed


def _connector(config: dict[str, Any], chunks: list[bytes] | None = None) -> TelnetSessionConnector:
    connector = TelnetSessionConnector("sess-1", "Demo Session", config)
    connector._transport = ScriptedTransport(chunks or [])
    return connector


async def _drive(config: dict[str, Any], chunks: list[bytes], steps: list[tuple[str, Any]]) -> dict[str, Any]:
    """Run one connector through a script and record what it produced."""
    connector = _connector(config, chunks)
    await connector.start()
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
        "sent": [bytes(chunk).decode("latin-1") for chunk in connector._transport.sent],
        "connected": connector.is_connected(),
    }


CONFIG_CASES: list[tuple[str, dict[str, Any]]] = [
    ("no settings at all", {}),
    ("a host and a port", {"host": "bbs.example", "port": 2323}),
    ("a port written as a string", {"port": "2323"}),
    ("a port written with spaces around it", {"port": " 2323 "}),
    ("a port that is not whole", {"port": "23.5"}),
    ("a port that is not a number", {"port": "telnet"}),
    ("a port written as a float", {"port": 2323.0}),
    ("the hijack input mode", {"input_mode": "hijack"}),
    ("the overlay turned off", {"hub_overlay": False}),
    ("private targets blocked", {"block_private_connector_targets": True}),
    ("a setting nobody defined", {"hostname": "bbs.example"}),
    ("a mistyped setting", {"prot": 2323}),
    ("two settings nobody defined", {"hostname": "h", "prot": 23}),
    ("a setting nobody defined alongside real ones", {"host": "h", "colour": "green"}),
]

DRIVE_CASES: list[tuple[str, dict[str, Any], list[bytes], list[tuple[str, Any]]]] = [
    ("a session that says nothing", {}, [], []),
    ("one chunk of output", {}, [b"Welcome to the BBS\r\n"], [("poll", None)]),
    ("output with high-byte drawing characters", {}, [bytes([0xC9, 0xCD, 0xBB, 0x0A])], [("poll", None)]),
    ("two chunks, counted", {}, [b"first\n", b"second\n"], [("poll", None), ("poll", None)]),
    ("nothing to read", {}, [b""], [("poll", None)]),
    ("input sent upstream", {}, [], [("input", "list\r\n")]),
    ("input the endpoint cannot spell", {}, [], [("input", "naïve\r\n")]),
    ("control taken and released", {}, [], [("control", "pause"), ("control", "resume")]),
    ("control taken and held", {}, [], [("control", "pause")]),
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
    (
        "a line wider than the screen",
        {"hub_overlay": False},
        [b"y" * 100 + b"\n"],
        [("poll", None)],
    ),
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
    """What the connector says about a mode it does not have."""
    connector = _connector({})
    try:
        await connector.set_mode("readonly")
    except ValueError as exc:
        return {"error": str(exc)}
    return {"error": None}


async def _blocked_peer() -> dict[str, Any]:
    """What happens when the endpoint answers from an address that is refused.

    The create-time guard checked the *name*; this checks the address actually
    reached, which is the mitigation for a name that resolves to one host and
    connects to another.
    """
    connector = _connector({})
    connector._transport = ScriptedTransport([], peer_ip="169.254.169.254")
    try:
        await connector.start()
    except Exception as exc:
        return {
            "error": type(exc).__name__,
            "connected": connector.is_connected(),
            "disconnects": connector._transport.disconnects,
        }
    return {"error": None, "connected": connector.is_connected(), "disconnects": 0}


async def _unknown_peer() -> dict[str, Any]:
    """A peer address nobody could determine is not itself a refusal."""
    connector = _connector({})
    connector._transport = ScriptedTransport([], peer_ip=None)
    await connector.start()
    return {"connected": connector.is_connected()}


async def main_async() -> None:
    config_outcomes = []
    for name, config in CONFIG_CASES:
        try:
            connector = _connector(config)
        except ValueError as exc:
            config_outcomes.append({"name": name, "config": config, "error": str(exc)})
            continue
        config_outcomes.append(
            {
                "name": name,
                "config": config,
                "error": None,
                "snapshot": _fix(await connector.get_snapshot()),
                "analysis": await connector.get_analysis(),
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
                "chunks": [chunk.decode("latin-1") for chunk in chunks],
                **await _drive(config, chunks, steps),
            }
            for name, config, chunks, steps in DRIVE_CASES
        ],
        "mode_refusal": await _mode_refusal(),
        "blocked_peer": await _blocked_peer(),
        "unknown_peer": await _unknown_peer(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['drive_cases'])} driven cases)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
