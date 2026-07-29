#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the session connect factories.

``connect_telnet`` and ``connect_ws`` are what a caller actually
reaches for, and each one decides two things worth recording:

* **The defaults a session gets when nobody says otherwise.** Eighty by
  twenty-five, ``ANSI``, CP437 on the way in — the shape a BBS expects, and
  not what a modern terminal library would pick on its own.
* **What reaches the transport.** The WebSocket factory omits ``origin`` and
  ``additional_headers`` entirely when they were not given rather than passing
  them as nothing, which is what lets a worker that gates cross-origin
  upgrades see the caller's real Origin. A port that always passed the keys
  would send ``None`` and be refused.

Both factories connect, so the corpus is recorded against transports that
answer without a network.

# uv-package: provide-uterm

Usage (from the repository root)::

    uv run --package provide-uterm python \\
        packages/provide-uterm-ts/testdata/gen_connectsession_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from provide.uterm import telnet_session, ws_session

OUT = Path(__file__).resolve().parent / "connectsession_golden.json"


class RecordingTransport:
    """A transport that records the call it was connected with."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.construction = {"args": [str(arg) for arg in args], "kwargs": _plain(kwargs)}
        self.connected_with: dict[str, Any] | None = None
        self.sent: list[bytes] = []
        self.closed = 0

    async def connect(self, *args: Any, **kwargs: Any) -> None:
        self.connected_with = {"args": [_plain(arg) for arg in args], "kwargs": _plain(kwargs)}

    async def receive(self, size: int = 4096, timeout_ms: int = 100) -> bytes:
        await asyncio.sleep(0.01)
        return b""

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def disconnect(self) -> None:
        self.closed += 1

    def is_connected(self) -> bool:
        return True


def _plain(value: Any) -> Any:
    """Whatever will round-trip through JSON, named rather than repr'd."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


async def _telnet(kwargs: dict[str, Any]) -> dict[str, Any]:
    transports: list[RecordingTransport] = []

    def build(*args: Any, **kw: Any) -> RecordingTransport:
        transport = RecordingTransport(*args, **kw)
        transports.append(transport)
        return transport

    with patch.object(telnet_session, "TelnetTransport", build):
        session = await telnet_session.connect_telnet("bbs.example", 2323, **kwargs)
        await session.send("hé\r")
        await session.close()
    transport = transports[0]
    return {
        "construction": transport.construction,
        "connected_with": transport.connected_with,
        "sent": [chunk.decode("latin-1") for chunk in transport.sent],
        "host": session.host,
        "port": session.port,
        "closes": transport.closed,
    }


async def _websocket(kwargs: dict[str, Any]) -> dict[str, Any]:
    transports: list[RecordingTransport] = []

    def build(*args: Any, **kw: Any) -> RecordingTransport:
        transport = RecordingTransport(*args, **kw)
        transports.append(transport)
        return transport

    with patch.object(ws_session, "WebSocketTransport", build):
        session = await ws_session.connect_ws("wss://feed.example/s", **kwargs)
        await session.send("hé\r")
        await session.close()
    transport = transports[0]
    return {
        "construction": transport.construction,
        "connected_with": transport.connected_with,
        "sent": [chunk.decode("latin-1") for chunk in transport.sent],
        "url": session.url,
        "closes": transport.closed,
    }


TELNET_CASES: list[tuple[str, dict[str, Any]]] = [
    ("nothing said", {}),
    ("a screen of its own", {"cols": 132, "rows": 43}),
    ("a terminal type of its own", {"term": "xterm-256color"}),
    ("a connect timeout of its own", {"connect_timeout": 5.0}),
    ("a receive codec of its own", {"receive_encoding": "latin-1"}),
    ("control frames parsed out", {"control_frames": True}),
]

WS_CASES: list[tuple[str, dict[str, Any]]] = [
    ("nothing said", {}),
    ("a screen of its own", {"cols": 132, "rows": 43}),
    ("an origin", {"origin": "https://app.example"}),
    ("headers", {"additional_headers": {"Authorization": "Bearer x"}}),  # pragma: allowlist secret
    ("an origin and headers", {"origin": "https://app.example", "additional_headers": {"X-Trace": "1"}}),
    ("timings of its own", {"ping_interval": 5, "ping_timeout": 6, "close_timeout": 7}),
    ("a text frame codec of its own", {"text_frame_encoding": "cp437"}),
    ("control frames parsed out", {"control_frames": True}),
]


async def main_async() -> None:
    corpus = {
        "telnet": [{"name": name, "kwargs": _plain(kwargs), **await _telnet(kwargs)} for name, kwargs in TELNET_CASES],
        "websocket": [
            {"name": name, "kwargs": _plain(kwargs), **await _websocket(kwargs)} for name, kwargs in WS_CASES
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['telnet'])} telnet, {len(corpus['websocket'])} websocket)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
