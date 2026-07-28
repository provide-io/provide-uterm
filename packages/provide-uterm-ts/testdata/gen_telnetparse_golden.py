#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for parsing a telnet byte stream.

Bytes arrive from a socket in whatever sizes the network chose, so a command
can be split across two reads. The parser separates payload from negotiation
and says how much it consumed, and the caller keeps the rest for next time.

**An incomplete command is not consumed.** It stays in the buffer until the
bytes that finish it arrive, which is the whole reason `consumed` is returned
separately from the payload — a parser that consumed a half-read command would
lose it.

**Unless the stream has ended.** With `final` set there will be no more bytes,
so a trailing partial command is emitted as payload rather than held forever:
half a negotiation is not worth losing the text before it.

**A doubled IAC is one literal byte.** That is how a payload byte of 255 is
carried, and a parser that missed it would read the second as the start of a
command.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_telnetparse_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.embed.telnet_upstream import escape_iac, parse_telnet_buffer

OUT = Path(__file__).with_name("telnetparse_golden.json")

IAC, WILL, WONT, DO, DONT, SB, SE = 255, 251, 252, 253, 254, 250, 240

# (name, bytes) — what escaping produces.
ESCAPE_CASES: list[tuple[str, bytes]] = [
    ("plain text", b"hello"),
    ("nothing", b""),
    ("one command byte", bytes((IAC,))),
    ("a command byte in text", b"a" + bytes((IAC,)) + b"b"),
    ("two command bytes", bytes((IAC, IAC))),
    ("every byte", bytes(range(256))),
]

# (name, bytes, final) — how a buffer parses.
PARSE_CASES: list[tuple[str, bytes, bool]] = [
    ("plain text", b"hello", False),
    ("nothing", b"", False),
    ("a negotiation", bytes((IAC, DO, 24)), False),
    ("a negotiation after text", b"hi" + bytes((IAC, WILL, 1)), False),
    ("a negotiation before text", bytes((IAC, WONT, 1)) + b"hi", False),
    ("two negotiations", bytes((IAC, DO, 24, IAC, DONT, 1)), False),
    ("a doubled command byte", b"a" + bytes((IAC, IAC)) + b"b", False),
    ("a subnegotiation", bytes((IAC, SB, 24, 1, 2, IAC, SE)), False),
    ("a subnegotiation with no body", bytes((IAC, SB, IAC, SE)), False),
    ("a subnegotiation with only an option", bytes((IAC, SB, 31, IAC, SE)), False),
    ("a subnegotiation around text", b"a" + bytes((IAC, SB, 24, 1, IAC, SE)) + b"b", False),
    ("a command nobody sends", bytes((IAC, 99)) + b"text", False),
    # Partial sequences, which is what a socket actually delivers.
    ("a trailing command byte", b"hi" + bytes((IAC,)), False),
    ("a trailing command byte, final", b"hi" + bytes((IAC,)), True),
    ("half a negotiation", b"hi" + bytes((IAC, DO)), False),
    ("half a negotiation, final", b"hi" + bytes((IAC, DO)), True),
    ("an unterminated subnegotiation", b"hi" + bytes((IAC, SB, 24, 1)), False),
    ("an unterminated subnegotiation, final", b"hi" + bytes((IAC, SB, 24, 1)), True),
    ("a subnegotiation ending on the last byte", bytes((IAC, SB, 24, IAC)), False),
    ("text then nothing else", b"hi", True),
    # A payload byte that looks like the start of a command.
    ("a doubled byte at the very end", b"a" + bytes((IAC, IAC)), False),
    ("a doubled byte split from its pair", b"a" + bytes((IAC,)), False),
]

# A stream delivered in pieces, to show the caller's loop working.
STREAM = b"hi" + bytes((IAC, DO, 24)) + b"there" + bytes((IAC, SB, 31, 0, 80, 0, 25, IAC, SE)) + b"!"


def _describe(result: tuple[bytes, list[tuple[bool, int, int, bytes]], int]) -> dict[str, Any]:
    """A parse as JSON carries it."""
    payload, events, consumed = result
    return {
        "payload": list(payload),
        "events": [{"is_sub": sub, "cmd": cmd, "opt": opt, "body": list(body)} for sub, cmd, opt, body in events],
        "consumed": consumed,
    }


def _stream_in_pieces(size: int) -> dict[str, Any]:
    """Feed the stream through in fixed-size reads, as a socket would."""
    buffer = b""
    payload = bytearray()
    events: list[dict[str, Any]] = []
    for start in range(0, len(STREAM), size):
        buffer += STREAM[start : start + size]
        final = start + size >= len(STREAM)
        chunk, chunk_events, consumed = parse_telnet_buffer(buffer, final=final)
        payload.extend(chunk)
        events.extend(
            {"is_sub": sub, "cmd": cmd, "opt": opt, "body": list(body)} for sub, cmd, opt, body in chunk_events
        )
        buffer = buffer[consumed:]
    return {"payload": list(payload), "events": events, "leftover": list(buffer)}


def _build() -> dict[str, Any]:
    """Everything the parser decides."""
    return {
        "escapes": [
            {"name": name, "input": list(data), "output": list(escape_iac(data))} for name, data in ESCAPE_CASES
        ],
        "parses": [
            {"name": name, "input": list(data), "final": final, **_describe(parse_telnet_buffer(data, final=final))}
            for name, data, final in PARSE_CASES
        ],
        "stream": list(STREAM),
        # The same stream read one byte at a time, three at a time, and whole.
        "stream_in_pieces": {str(size): _stream_in_pieces(size) for size in (1, 3, len(STREAM))},
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(ESCAPE_CASES)} escapes, {len(PARSE_CASES)} parses)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
