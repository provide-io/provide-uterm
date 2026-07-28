#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript telnet port.

RFC 854 in-band signalling: the command byte is 0xFF, which is also a byte a
terminal legitimately sends, so every layer has to agree on where a command
starts and ends. Getting that wrong does not fail loudly — it puts stray
command bytes on the operator's screen, or worse, swallows screen content as
though it were a negotiation.

Three things are recorded.

**Parsing.** Payload, events and the number of bytes consumed, for every
shape a stream can arrive in — including partial sequences split across
reads, which is the normal case on a socket and the one a naive parser
mishandles by treating a trailing 0xFF as data.

**Framing at the end of a stream.** A truncated sequence is held back while
more may arrive, and emitted as literal data once nothing will. Those two
answers differ for the same input, so both are recorded.

**The negotiation replies.** Which options are accepted and which refused
decides whether the far end sends window-size updates and terminal-type
queries at all.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_telnet_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.transports._telnet_const import (
    DO,
    DONT,
    IAC,
    NAWS,
    OPT_BINARY,
    OPT_ECHO,
    OPT_SGA_OPT,
    OPT_TTYPE,
    SB,
    SE,
    WILL,
    WONT,
)
from provide.uterm.transports.telnet_transport import TelnetTransport

OUT = Path(__file__).with_name("telnet_golden.json")

# (name, bytes on the wire)
PARSE_CASES: list[tuple[str, list[int]]] = [
    ("empty", []),
    ("plain text", [104, 105]),
    ("escaped command byte", [IAC, IAC]),
    ("text around an escaped command byte", [97, IAC, IAC, 98]),
    ("do", [IAC, DO, NAWS]),
    ("dont", [IAC, DONT, NAWS]),
    ("will", [IAC, WILL, OPT_ECHO]),
    ("wont", [IAC, WONT, OPT_ECHO]),
    ("negotiation between text", [97, IAC, DO, NAWS, 98]),
    ("two negotiations", [IAC, DO, NAWS, IAC, WILL, OPT_ECHO]),
    ("subnegotiation", [IAC, SB, OPT_TTYPE, 1, IAC, SE]),
    ("subnegotiation between text", [97, IAC, SB, OPT_TTYPE, 1, IAC, SE, 98]),
    ("empty subnegotiation", [IAC, SB, IAC, SE]),
    ("unknown command is dropped", [97, IAC, 99, 98]),
    # Partial sequences: the normal case on a socket.
    ("trailing command byte", [97, IAC]),
    ("truncated negotiation", [97, IAC, DO]),
    ("truncated subnegotiation", [97, IAC, SB, OPT_TTYPE]),
    ("subnegotiation with no end", [IAC, SB, OPT_TTYPE, 1, 2, 3]),
    ("high bytes are data", [128, 200, 254]),
    ("a command byte inside a subnegotiation payload", [IAC, SB, OPT_TTYPE, 0, 65, IAC, SE]),
]

# (name, bytes to send)
SEND_CASES: list[tuple[str, list[int]]] = [
    ("plain", [104, 105]),
    ("one command byte", [IAC]),
    ("command byte among text", [97, IAC, 98]),
    ("two command bytes", [IAC, IAC]),
    ("no bytes", []),
    ("high bytes", [128, 254]),
]


def _parse(data: list[int], final: bool) -> dict[str, Any]:
    """Run one buffer through the parser."""
    payload, events, consumed = TelnetTransport._parse_telnet_buffer(bytes(data), final=final)
    return {
        "payload": list(payload),
        "events": [[kind, code, list(value) if isinstance(value, bytes) else value] for kind, code, value in events],
        "consumed": consumed,
    }


def main() -> int:
    """Write the golden corpus and report the case count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_telnet_golden.py",
        "constants": {
            "IAC": IAC,
            "WILL": WILL,
            "WONT": WONT,
            "DO": DO,
            "DONT": DONT,
            "SB": SB,
            "SE": SE,
            "OPT_BINARY": OPT_BINARY,
            "OPT_ECHO": OPT_ECHO,
            "OPT_SGA": OPT_SGA_OPT,
            "OPT_NAWS": NAWS,
            "OPT_TTYPE": OPT_TTYPE,
        },
        "parses": [
            {
                "name": name,
                "bytes": data,
                "streaming": _parse(data, final=False),
                "final": _parse(data, final=True),
            }
            for name, data in PARSE_CASES
        ],
        "sends": [
            {"name": name, "bytes": data, "escaped": list(bytes(data).replace(bytes([IAC]), bytes([IAC, IAC])))}
            for name, data in SEND_CASES
        ],
        # Which options the client accepts, and which it refuses.
        "do_accepts": [OPT_BINARY, OPT_SGA_OPT, NAWS, OPT_TTYPE],
        "will_accepts": [OPT_ECHO, OPT_SGA_OPT, OPT_BINARY],
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(payload['parses'])} parse cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
