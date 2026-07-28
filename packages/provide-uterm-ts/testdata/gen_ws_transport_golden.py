#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript WebSocket transport.

The corpus is recorded by driving the *real* ``WebSocketTransport`` against a
fake socket, so what is pinned is the reference's behaviour rather than a
second reading of it.

Three things here are easy to get wrong and silent when wrong:

* **Frame opcode.** The library maps ``str`` to TEXT and ``bytes`` to BINARY.
  The control plane and the Cloudflare Worker speak TEXT; a BINARY frame
  arrives at the worker as a ``JsProxy`` and is dropped without an error. So
  outgoing bytes are decoded back to text before the send.
* **The wire dialect of an incoming text frame.** Byte-oriented BBS gateways
  put each terminal byte in the same-valued code point, hence the latin-1
  default; a UTF-8 decode there would turn a CP437 box-drawing byte into a
  replacement character.
* **Which failures tear the connection down.** A read timeout must yield no
  data and leave the connection up; anything else disconnects first, so the
  caller cannot keep using a socket that is already gone.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_ws_transport_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import websockets
from provide.uterm.transports.ws_transport import WebSocketTransport
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

OUT = Path(__file__).with_name("ws_transport_golden.json")

# (name, host, port, kwargs) — the URL a connect resolves to, and which of the
# caller's options reach the library.
CONNECT_CASES: list[tuple[str, str, int, dict[str, Any]]] = [
    ("host and port", "bbs.example.org", 2323, {}),
    ("explicit url wins", "ignored", 1, {"url": "ws://localhost:8080/ws"}),
    ("empty url falls back", "bbs.example.org", 2323, {"url": ""}),
    ("forwards the tuning options", "h", 1, {"max_size": 65536, "ping_interval": 20.0}),
    ("forwards origin and headers", "h", 1, {"origin": "https://app.example.org", "additional_headers": {"UA": "bot"}}),
    ("drops an option left unset", "h", 1, {"max_size": None, "ping_timeout": 5.0}),
    ("drops an option it does not know", "h", 1, {"compression": "deflate"}),
    ("keeps a zero", "h", 1, {"ping_interval": 0, "close_timeout": 0.0}),
]

# (name, bytes to send) — what lands in the TEXT frame.
SEND_CASES: list[tuple[str, bytes]] = [
    ("ascii", b"ls -la\r"),
    ("utf-8 text", "héllo → ✓".encode()),
    ("a byte that is not valid utf-8", b"a\xffb"),
    ("a truncated multi-byte character", b"caf\xc3"),
    ("a lone continuation byte", b"\x80"),
    ("empty", b""),
]

# (name, message) — a text frame is re-encoded to terminal bytes, a binary
# frame already is them.
RECEIVE_CASES: list[tuple[str, Any]] = [
    ("ascii text frame", "hello"),
    ("high code points in a text frame", "─│É"),
    ("cp437 box drawing as latin-1 code points", "ÉÍ»"),
    # The last code point that still fits latin-1. An off-by-one at the
    # boundary substitutes it instead, and the corpus is the only thing that
    # would notice.
    ("the top of the latin-1 range", "ÿ"),
    ("binary frame", b"\x1b[2J\xff"),
    ("empty text frame", ""),
]


class FakeSocket:
    """Stands in for a ``websockets`` client connection."""

    def __init__(self, *, incoming: list[Any] | None = None) -> None:
        self.sent: list[Any] = []
        self.incoming = list(incoming or [])
        self.state = State.OPEN
        self.closed = False

    async def send(self, message: Any) -> None:
        """Record what was sent, and its type — TEXT versus BINARY."""
        self.sent.append(message)

    async def recv(self) -> Any:
        """Yield the next queued message, or block forever once drained."""
        if not self.incoming:
            await asyncio.Event().wait()
        return self.incoming.pop(0)

    async def close(self) -> None:
        """Mark the socket closed."""
        self.closed = True
        self.state = State.CLOSED


def _closed_error() -> ConnectionClosed:
    """A ``ConnectionClosed`` the same shape the library raises."""
    return ConnectionClosed(None, None)


def _cause_chain(exc: BaseException) -> list[str]:
    """The exception type names from `exc` down through its causes."""
    names = [type(exc).__name__]
    while exc.__cause__ is not None:
        exc = exc.__cause__
        names.append(type(exc).__name__)
    return names


async def _record_connects(patched: dict[str, Any]) -> list[dict[str, Any]]:
    """Drive `connect` for every case, capturing URL and forwarded options."""
    records = []
    for name, host, port, kwargs in CONNECT_CASES:
        transport = WebSocketTransport()
        patched["seen"] = None
        await transport.connect(host, port, **kwargs)
        seen = patched["seen"]
        records.append(
            {
                "name": name,
                "url": seen["url"],
                # Sorted so the record is about which options survive, not the
                # order the reference happens to iterate them in.
                "forwarded": {key: seen["kwargs"][key] for key in sorted(seen["kwargs"])},
                "connected": transport.is_connected(),
            }
        )
    return records


async def _record_connect_failure(patched: dict[str, Any]) -> dict[str, Any]:
    """Drive a failing `connect`, capturing the message and the cause chain."""
    patched["fail"] = OSError("no route to host")
    transport = WebSocketTransport()
    try:
        await transport.connect("down.example.org", 2323)
    except ConnectionError as exc:
        record = {"message": str(exc), "causes": _cause_chain(exc), "connected": transport.is_connected()}
    finally:
        patched["fail"] = None
    return record


async def _record_sends(patched: dict[str, Any]) -> list[dict[str, Any]]:
    """Drive `send` for every case, capturing the frame that reached the wire."""
    records = []
    for name, payload in SEND_CASES:
        transport = WebSocketTransport()
        await transport.connect("h", 1)
        socket = patched["socket"]
        await transport.send(payload)
        (frame,) = socket.sent
        records.append(
            {
                "name": name,
                "bytes": list(payload),
                "text": frame,
                # TEXT versus BINARY is the whole point: a BINARY frame is
                # dropped by the worker without an error.
                "is_text": isinstance(frame, str),
            }
        )
    return records


async def _record_receives(patched: dict[str, Any]) -> list[dict[str, Any]]:
    """Drive `receive` for every case, in both wire dialects."""
    records = []
    for name, message in RECEIVE_CASES:
        row: dict[str, Any] = {"name": name, "is_text": isinstance(message, str)}
        row["message"] = message if isinstance(message, str) else list(message)
        for encoding in ("latin-1", "utf-8"):
            transport = WebSocketTransport(text_frame_encoding=encoding)
            await transport.connect("h", 1)
            patched["socket"].incoming.append(message)
            row[encoding] = list(await transport.receive(4096, 1000))
        records.append(row)
    return records


async def _record_failures(patched: dict[str, Any]) -> dict[str, Any]:
    """Drive every failure path, capturing message and resulting liveness."""
    failures: dict[str, Any] = {}

    # Sending or receiving before a connect.
    fresh = WebSocketTransport()
    for method, call in (("send", fresh.send(b"x")), ("receive", fresh.receive(1, 1))):
        try:
            await call
        except ConnectionError as exc:
            failures[f"{method}_not_connected"] = str(exc)

    # A close that arrives while sending.
    transport = WebSocketTransport()
    await transport.connect("h", 1)
    patched["socket"].send_error = _closed_error()
    try:
        await transport.send(b"x")
    except ConnectionError as exc:
        failures["send_closed"] = {"message": str(exc), "connected_after": transport.is_connected()}

    # A close that arrives while receiving, versus any other read fault.
    for key, error in (("receive_closed", _closed_error()), ("receive_error", ValueError("frame too large"))):
        transport = WebSocketTransport()
        await transport.connect("h", 1)
        patched["socket"].recv_error = error
        try:
            await transport.receive(4096, 1000)
        except ConnectionError as exc:
            failures[key] = {"message": str(exc), "connected_after": transport.is_connected()}

    # A read timeout yields no data and leaves the connection up: a quiet
    # terminal is not a broken one.
    transport = WebSocketTransport()
    await transport.connect("h", 1)
    failures["receive_timeout"] = {
        "data": list(await transport.receive(4096, 10)),
        "connected_after": transport.is_connected(),
    }

    # A reconnect that fails. The old socket is still attached — nothing
    # clears it — so liveness and the send guard have to be reading the
    # connected flag, not merely the presence of a socket. Otherwise the
    # caller keeps writing into a connection that is already gone.
    transport = WebSocketTransport()
    await transport.connect("h", 1)
    patched["fail"] = OSError("no route to host")
    try:
        await transport.connect("h", 1)
    except ConnectionError:
        pass
    finally:
        patched["fail"] = None
    reconnect: dict[str, Any] = {"connected_after": transport.is_connected()}
    try:
        await transport.send(b"x")
    except ConnectionError as exc:
        reconnect["send_message"] = str(exc)
    try:
        await transport.receive(4096, 10)
    except ConnectionError as exc:
        reconnect["receive_message"] = str(exc)
    failures["failed_reconnect"] = reconnect

    # Disconnect is idempotent, and survives a socket that fails to close.
    transport = WebSocketTransport()
    await transport.connect("h", 1)
    patched["socket"].close_error = RuntimeError("already gone")
    await transport.disconnect()
    await transport.disconnect()
    failures["disconnect_idempotent"] = {"connected_after": transport.is_connected()}

    return failures


async def _record_liveness(patched: dict[str, Any]) -> list[dict[str, Any]]:
    """Capture `is_connected` against each socket state."""
    records = []
    for state in (State.CONNECTING, State.OPEN, State.CLOSING, State.CLOSED):
        transport = WebSocketTransport()
        await transport.connect("h", 1)
        patched["socket"].state = state
        records.append({"state": state.name, "connected": transport.is_connected()})
    records.append({"state": "never connected", "connected": WebSocketTransport().is_connected()})
    return records


class _Fake(FakeSocket):
    """A fake socket that can be told to fail a specific call."""

    send_error: BaseException | None = None
    recv_error: BaseException | None = None
    close_error: BaseException | None = None

    async def send(self, message: Any) -> None:
        """Send, or raise what the case asked for."""
        if self.send_error is not None:
            raise self.send_error
        await super().send(message)

    async def recv(self) -> Any:
        """Receive, or raise what the case asked for."""
        if self.recv_error is not None:
            raise self.recv_error
        return await super().recv()

    async def close(self) -> None:
        """Close, or raise what the case asked for."""
        if self.close_error is not None:
            raise self.close_error
        await super().close()


async def _main() -> int:
    """Write the golden corpus and report the case count."""
    patched: dict[str, Any] = {"seen": None, "socket": None, "fail": None}

    async def fake_connect(url: str, **kwargs: Any) -> Any:
        """Stand in for ``websockets.connect``."""
        patched["seen"] = {"url": url, "kwargs": kwargs}
        if patched["fail"] is not None:
            raise patched["fail"]
        socket = _Fake()
        patched["socket"] = socket
        return socket

    original = websockets.connect
    websockets.connect = fake_connect  # type: ignore[assignment]
    try:
        corpus = {
            "connects": await _record_connects(patched),
            "connect_failure": await _record_connect_failure(patched),
            "sends": await _record_sends(patched),
            "receives": await _record_receives(patched),
            "failures": await _record_failures(patched),
            "liveness": await _record_liveness(patched),
            "default_text_frame_encoding": WebSocketTransport()._text_frame_encoding,
        }
    finally:
        websockets.connect = original  # type: ignore[assignment]

    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['connects'])} connects, {len(corpus['sends'])} sends)")
    return 0


def main() -> int:
    """Entry point."""
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
