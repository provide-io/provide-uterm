#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the WebSocket session connector.

The sibling of the telnet connector, and it differs in three ways worth
recording:

* **The endpoint is required and its scheme is checked.** A session with no
  URL, or one pointing at ``http://``, is refused at construction rather than
  failing on connect — so a config mistake is a server that will not start
  rather than a session that exists and never works.
* **Two kinds of frame arrive.** A binary frame is read as CP437 and counted
  in bytes; a text frame is taken as it is and counted in UTF-8 bytes. The
  distinction shows up in the byte count a viewer reads.
* **A read that times out is not a disconnection.** A poll with nothing to
  read says nothing at all; a poll on a closed socket says so once and leaves
  the session down.

The connector is driven with a socket that answers from a script, so the
recorded stream is the connector's own behaviour rather than a network's.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_wsconnector_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from provide.uterm.server.connectors.websocket import WebSocketSessionConnector

OUT = Path(__file__).resolve().parent / "wsconnector_golden.json"

# Every recorded timestamp is replaced with this, so the corpus does not differ
# from itself on every run.
FIXED_TS = 1_700_000_000.0

URL = "wss://feed.example/session"


class ScriptedSocket:
    """A WebSocket that answers from a list rather than a network."""

    def __init__(self, frames: list[Any], peer: tuple[str, int] | None = ("203.0.113.7", 443)) -> None:
        self._frames = list(frames)
        self.remote_address = peer
        self.sent: list[str] = []
        self.closed = 0

    async def recv(self) -> Any:
        if not self._frames:
            # Nothing to read is a timeout, which is not a disconnection.
            raise TimeoutError
        frame = self._frames.pop(0)
        if frame is None:
            raise ConnectionError("socket closed")
        return frame

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed += 1


def _fix(message: dict[str, Any]) -> dict[str, Any]:
    fixed = dict(message)
    if "ts" in fixed:
        fixed["ts"] = FIXED_TS
    return fixed


def _connector(config: dict[str, Any]) -> WebSocketSessionConnector:
    return WebSocketSessionConnector("sess-1", "Demo Session", {"url": URL, **config})


async def _started(config: dict[str, Any], frames: list[Any]) -> WebSocketSessionConnector:
    """A connector already connected to a scripted socket."""
    connector = _connector(config)
    connector._ws = ScriptedSocket(frames)
    connector._connected = True
    connector._banner = f"Connected to {URL}"
    return connector


async def _drive(config: dict[str, Any], frames: list[Any], steps: list[tuple[str, Any]]) -> dict[str, Any]:
    connector = await _started(config, frames)
    socket = connector._ws
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
        "sent": list(socket.sent),
        "closes": socket.closed,
        "connected": connector.is_connected(),
    }


CONFIG_CASES: list[tuple[str, dict[str, Any]]] = [
    ("a secure endpoint", {"url": "wss://feed.example/session"}),
    ("a cleartext endpoint", {"url": "ws://feed.example/session"}),
    ("no endpoint at all", {}),
    ("an endpoint given null", {"url": None}),
    ("an endpoint that is empty", {"url": ""}),
    ("an endpoint served over http", {"url": "http://feed.example/session"}),
    ("an endpoint with no scheme", {"url": "feed.example/session"}),
    ("an endpoint with no host", {"url": "wss:///session"}),
    ("an endpoint with a port and a query", {"url": "wss://feed.example:8443/s?token=x"}),
    ("an endpoint at an IPv6 address", {"url": "wss://[2001:db8::1]:443/s"}),
    ("an endpoint carrying credentials", {"url": "wss://user:pass@feed.example/s"}),  # pragma: allowlist secret
    ("an endpoint whose scheme is shouted", {"url": "WSS://feed.example/s"}),
    ("an endpoint that is only a scheme", {"url": "wss://"}),
    ("the hijack input mode", {"url": URL, "input_mode": "hijack"}),
    ("the overlay turned off", {"url": URL, "hub_overlay": False}),
    ("private targets blocked", {"url": URL, "block_private_connector_targets": True}),
    ("a setting nobody defined", {"url": URL, "endpoint": "wss://other.example"}),
    ("two settings nobody defined", {"url": URL, "endpoint": "x", "mode": "open"}),
]

DRIVE_CASES: list[tuple[str, dict[str, Any], list[Any], list[tuple[str, Any]]]] = [
    ("a session that says nothing", {}, [], []),
    ("a text frame", {}, ["hello from upstream\n"], [("poll", None)]),
    ("a text frame with characters outside ASCII", {}, ["héllo ☃\n"], [("poll", None)]),
    ("a binary frame", {}, [bytes([0xC9, 0xCD, 0xBB, 0x0A])], [("poll", None)]),
    ("both kinds of frame, counted", {}, ["abc", bytes([0xDB, 0x0A])], [("poll", None), ("poll", None)]),
    ("nothing to read", {}, [], [("poll", None)]),
    ("a socket that has closed", {}, [None], [("poll", None)]),
    ("a socket that closes after a frame", {}, ["first\n", None], [("poll", None), ("poll", None)]),
    ("a poll after the socket closed", {}, [None], [("poll", None), ("poll", None)]),
    ("input sent upstream", {}, [], [("input", "list\r\n")]),
    ("input outside ASCII sent upstream", {}, [], [("input", "naïve\r\n")]),
    ("control taken and held", {}, [], [("control", "pause")]),
    ("control taken and released", {}, [], [("control", "pause"), ("control", "resume")]),
    ("a step requested", {}, [], [("control", "step")]),
    ("a control action nobody defined", {}, [], [("control", "rewind")]),
    ("the mode changed to hijack", {}, [], [("mode", "hijack")]),
    ("the mode changed back", {"input_mode": "hijack"}, [], [("mode", "open")]),
    ("the screen cleared", {}, ["some output\n"], [("poll", None), ("clear", None)]),
    ("the overlay turned off", {"hub_overlay": False}, ["bare output\n"], [("poll", None)]),
    (
        "more output than the buffer holds",
        {"hub_overlay": False},
        ["FIRST\n" + "x" * 33_000 + "\nLAST\n"],
        [("poll", None)],
    ),
    ("a line wider than the screen", {"hub_overlay": False}, ["y" * 100 + "\n"], [("poll", None)]),
    (
        "more lines than the screen has, with no overlay",
        {"hub_overlay": False},
        ["\n".join(f"row {index}" for index in range(30))],
        [("poll", None)],
    ),
    (
        "more output than the screen holds",
        {},
        ["\n".join(f"line {index}" for index in range(40))],
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


async def _peer_case(peer: tuple[str, int] | None, block_private: bool = False) -> dict[str, Any]:
    """What the post-connect address check does with this peer."""
    connector = _connector({"block_private_connector_targets": block_private} if block_private else {})
    socket = ScriptedSocket([], peer=peer)
    try:
        await connector._assert_peer_allowed(socket)
    except Exception as exc:
        return {"error": type(exc).__name__, "closes": socket.closed}
    return {"error": None, "closes": socket.closed}


async def _stop_case() -> dict[str, Any]:
    connector = await _started({}, [])
    socket = connector._ws
    await connector.stop()
    return {"closes": socket.closed, "connected": connector.is_connected()}


async def main_async() -> None:
    config_outcomes = []
    for name, config in CONFIG_CASES:
        try:
            connector = WebSocketSessionConnector("sess-1", "Demo Session", config)
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
        "url": URL,
        "parsed_urls": {
            url: {"scheme": urlparse(url).scheme, "host": urlparse(url).hostname or ""}
            for url in (
                "wss://feed.example/session",
                "ws://feed.example/session",
                "wss://feed.example:8443/s?token=x",
                "wss://[2001:db8::1]:443/s",
                "wss://user:pass@feed.example/s",  # pragma: allowlist secret
                "WSS://feed.example/s",
                "wss:///session",
                "wss://",
                "feed.example/session",
                "http://feed.example/session",
                "",
            )
        },
        "cols": 80,
        "rows": 25,
        "config_cases": config_outcomes,
        "drive_cases": [
            {
                "name": name,
                "config": config,
                "frames": [
                    {"binary": True, "data": frame.decode("latin-1")}
                    if isinstance(frame, bytes)
                    else ({"closed": True} if frame is None else {"binary": False, "data": frame})
                    for frame in frames
                ],
                **await _drive(config, frames, steps),
            }
            for name, config, frames, steps in DRIVE_CASES
        ],
        "mode_refusal": await _mode_refusal(),
        "blocked_peer": await _peer_case(("169.254.169.254", 443)),
        "unknown_peer": await _peer_case(None),
        "private_peer_allowed": await _peer_case(("10.0.0.5", 443)),
        "private_peer_blocked": await _peer_case(("10.0.0.5", 443), block_private=True),
        "stop": await _stop_case(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['drive_cases'])} driven cases)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
