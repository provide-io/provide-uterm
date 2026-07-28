#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript telnet transport.

The framing is already pinned by the telnet corpus; what this records is the
*conversation* — what the transport says on connect, how it answers a
negotiation, and what it does when the far end misbehaves.

* **The opening offer.** A client that says nothing gets a line-mode,
  non-binary session, and a BBS then draws for the wrong terminal.
* **The answers.** Agreeing to NAWS and then never sending the size leaves the
  server guessing; agreeing to TTYPE and never sending the type does the same.
  Both are followed immediately by the value.
* **What goes out on the wire.** Every `0xFF` a user types is doubled, or the
  far end reads it as the start of a command — and DEL is remapped to
  backspace, because that is what a BBS deletes with.
* **A hostile peer.** A subnegotiation that never ends would grow the receive
  buffer without bound, so there is a cap and reaching it is fatal.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_telnet_transport_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.transports import telnet_transport as module
from provide.uterm.transports._telnet_const import (
    DO,
    DONT,
    IAC,
    OPT_BINARY,
    OPT_ECHO,
    OPT_NAWS,
    OPT_SGA_OPT,
    OPT_TTYPE,
    SB,
    SE,
    WILL,
    WONT,
)

OUT = Path(__file__).with_name("telnet_transport_golden.json")

COLS = 80
ROWS = 25
TERM = "ANSI"


class FakeWriter:
    """Collects everything the transport writes."""

    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str) -> Any:
        return ("203.0.113.7", 2323) if name == "peername" else None


class FakeReader:
    """Hands out queued chunks, then end-of-stream."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""


async def _connected(chunks: list[bytes] | None = None):
    """A transport wired to a fake socket, already connected."""
    reader, writer = FakeReader(chunks or []), FakeWriter()

    async def fake_open(host: str, port: int) -> Any:
        return reader, writer

    real = asyncio.open_connection
    asyncio.open_connection = fake_open  # type: ignore[assignment]
    try:
        transport = module.TelnetTransport()
        await transport.connect("bbs.example.org", 2323, cols=COLS, rows=ROWS, term=TERM)
    finally:
        asyncio.open_connection = real  # type: ignore[assignment]
    writer.written.clear()
    return transport, reader, writer


async def _record_handshake() -> dict[str, Any]:
    """What the transport says before anybody says anything to it."""
    reader, writer = FakeReader([]), FakeWriter()

    async def fake_open(host: str, port: int) -> Any:
        return reader, writer

    real = asyncio.open_connection
    asyncio.open_connection = fake_open  # type: ignore[assignment]
    try:
        transport = module.TelnetTransport()
        await transport.connect("bbs.example.org", 2323, cols=COLS, rows=ROWS, term=TERM)
    finally:
        asyncio.open_connection = real  # type: ignore[assignment]
    return {"opening_offer": list(writer.written), "connected": transport.is_connected()}


async def _record_answer(name: str, incoming: bytes) -> dict[str, Any]:
    """What the transport writes back when handed `incoming`."""
    transport, _reader, writer = await _connected([incoming])
    payload = await transport.receive(4096, 100)
    # The negotiation replies are dispatched as tasks; let them run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return {"name": name, "incoming": list(incoming), "payload": list(payload), "reply": list(writer.written)}


async def _record_sends() -> list[dict[str, Any]]:
    """What reaches the wire for a given input."""
    cases: list[tuple[str, bytes]] = [
        ("plain text", b"ls -la\r"),
        ("a command byte", b"a\xffb"),
        ("only command bytes", b"\xff\xff"),
        ("a delete", b"abc\x7f"),
        ("a delete and a command byte", b"\x7f\xff"),
        ("empty", b""),
        ("high bytes that are not commands", bytes([0x80, 0xC8, 0xFE])),
    ]
    records = []
    for name, payload in cases:
        transport, _reader, writer = await _connected()
        await transport.send(payload)
        records.append({"name": name, "input": list(payload), "wire": list(writer.written)})
    return records


async def _record_receives() -> list[dict[str, Any]]:
    """What comes back out of the stream."""
    cases: list[tuple[str, list[bytes]]] = [
        ("plain text", [b"hello"]),
        ("a doubled command byte", [bytes([IAC, IAC])]),
        ("a negotiation between text", [bytes([97, IAC, DO, OPT_BINARY, 98])]),
        ("a sequence split across reads", [bytes([97, IAC]), bytes([DO, OPT_BINARY])]),
        ("a subnegotiation", [bytes([IAC, SB, OPT_TTYPE, 1, IAC, SE])]),
    ]
    records = []
    for name, chunks in cases:
        transport, _reader, _writer = await _connected(chunks)
        payloads = []
        for _ in chunks:
            payloads.append(list(await transport.receive(4096, 100)))
        await asyncio.sleep(0)
        records.append({"name": name, "chunks": [list(chunk) for chunk in chunks], "payloads": payloads})
    return records


async def _failure(coro: Any) -> str | None:
    """Await and name the refusal, or None."""
    try:
        await coro
    except ConnectionError as exc:
        return str(exc)
    return None


async def _record_failures() -> dict[str, Any]:
    """Every way the transport refuses."""
    fresh = module.TelnetTransport()
    results = {
        "send_before_connect": await _failure(fresh.send(b"x")),
        "receive_before_connect": await _failure(fresh.receive(1, 1)),
        "set_size_before_connect": await _failure(fresh.set_size(100, 40)),
    }

    # End of stream with nothing buffered.
    closed, _reader, _writer = await _connected([b""])
    results["closed_with_nothing_buffered"] = await _failure(closed.receive(4096, 100))
    results["closed_leaves_it_disconnected"] = closed.is_connected()

    # End of stream with a partial sequence buffered: the bytes are handed over
    # rather than dropped.
    trailing, _reader, _writer = await _connected([bytes([97, IAC]), b""])
    first = list(await trailing.receive(4096, 100))
    second = list(await trailing.receive(4096, 100))
    results["partial_then_close"] = {"first": first, "second": second}

    # A subnegotiation that never ends.
    flood = bytes([IAC, SB, OPT_TTYPE]) + b"x" * (256 * 1024 + 16)
    hostile, _reader, _writer = await _connected([flood])
    results["unbounded_subnegotiation"] = await _failure(hostile.receive(len(flood) + 16, 100))
    return results


async def _record_sequences() -> dict[str, Any]:
    """Behaviours that only show across more than one message."""
    # An incoming WILL records the option, so this end does not then send its
    # own WILL for it — that is what stops two polite implementations
    # negotiating at each other forever.
    suppressed, _reader, writer = await _connected([bytes([IAC, WILL, OPT_NAWS]), bytes([IAC, DO, OPT_NAWS])])
    await suppressed.receive(4096, 100)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    writer.written.clear()
    await suppressed.receive(4096, 100)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    incoming_suppresses_outgoing = list(writer.written)

    # A resize is remembered, so the next NAWS negotiation reports the new
    # size rather than the one from connect.
    resized, _reader, resized_writer = await _connected([bytes([IAC, DO, OPT_NAWS])])
    await resized.set_size(132, 43)
    resized_writer.written.clear()
    await resized.receive(4096, 100)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    after_resize = list(resized_writer.written)

    # A size whose bytes include the command byte: the payload is escaped or
    # the receiver reads it as framing and the block ends in the wrong place.
    escaped, _reader, escaped_writer = await _connected()
    await escaped.set_size(255, 25)
    return {
        "incoming_will_suppresses_our_will": incoming_suppresses_outgoing,
        "naws_after_resize": after_resize,
        "naws_with_a_command_byte": list(escaped_writer.written),
    }


async def _record_set_size() -> dict[str, Any]:
    """What a resize puts on the wire."""
    transport, _reader, writer = await _connected()
    await transport.set_size(132, 43)
    return {"cols": 132, "rows": 43, "wire": list(writer.written)}


async def _main() -> int:
    """Write the golden corpus and report the case count."""
    answers = [
        await _record_answer("do binary", bytes([IAC, DO, OPT_BINARY])),
        await _record_answer("do sga", bytes([IAC, DO, OPT_SGA_OPT])),
        await _record_answer("do naws", bytes([IAC, DO, OPT_NAWS])),
        await _record_answer("do ttype", bytes([IAC, DO, OPT_TTYPE])),
        await _record_answer("do echo", bytes([IAC, DO, OPT_ECHO])),
        await _record_answer("do something unknown", bytes([IAC, DO, 99])),
        await _record_answer("will echo", bytes([IAC, WILL, OPT_ECHO])),
        await _record_answer("will sga", bytes([IAC, WILL, OPT_SGA_OPT])),
        await _record_answer("will something unknown", bytes([IAC, WILL, 99])),
        await _record_answer("dont binary", bytes([IAC, DONT, OPT_BINARY])),
        await _record_answer("wont echo", bytes([IAC, WONT, OPT_ECHO])),
        await _record_answer("a ttype request", bytes([IAC, SB, OPT_TTYPE, 1, IAC, SE])),
        # Subnegotiations this end has nothing to say about: a different
        # option, one with no sub-command, and an empty one.
        await _record_answer("a naws subnegotiation", bytes([IAC, SB, OPT_NAWS, 0, 80, 0, 25, IAC, SE])),
        await _record_answer("a ttype block with no sub-command", bytes([IAC, SB, OPT_TTYPE, IAC, SE])),
        await _record_answer("an empty subnegotiation", bytes([IAC, SB, IAC, SE])),
    ]
    corpus = {
        "cols": COLS,
        "rows": ROWS,
        "term": TERM,
        "handshake": await _record_handshake(),
        "answers": answers,
        "sends": await _record_sends(),
        "receives": await _record_receives(),
        "failures": await _record_failures(),
        "set_size": await _record_set_size(),
        "sequences": await _record_sequences(),
        "max_rx_buffer": 256 * 1024,
        "default_connect_timeout_s": 30.0,
        "peer_ip": (await _connected())[0].peer_ip(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(answers)} answers, {len(corpus['sends'])} sends)")
    return 0


def main() -> int:
    """Entry point."""
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
