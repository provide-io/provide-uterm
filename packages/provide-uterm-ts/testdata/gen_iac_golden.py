#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the telnet gateway's negotiation.

A telnet client and the gateway have to agree on two things before anything
useful crosses: what kind of terminal is at the far end, and what it can
show. Both arrive as IAC subnegotiations, and getting them wrong is a session
rendered in the wrong colours or a stream with protocol bytes left in it.

* **Every IAC byte is taken out of the stream.** What the client typed is
  what goes upstream; a stray `IAC` reaching a shell is a byte nobody typed.
* **Only the two options this end asked for are answered.** A client
  accepting `TTYPE` or `NEW-ENVIRON` is asked to send it; every other verb —
  including a client offering something nobody wants — gets silence rather
  than a refusal. Recorded because it is not what RFC 854 asks for: a client
  that waits for `DONT` waits forever. Whether any client in practice does is
  not something this port can answer, so it does what the reference does and
  says so here.
* **A subnegotiation can arrive in pieces**, since it is a byte stream, so
  the negotiator has to hold what it has and carry on.
* **`TERM` and `COLORTERM` decide the colour mode** the upstream session is
  opened with.

Driven byte by byte and in whole chunks both, because a state machine that
works on one and not the other is a state machine that works only in tests.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_iac_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.gateway import _iac_negotiate as iac

OUT = Path(__file__).resolve().parent / "iac_golden.json"

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
TTYPE, NEW_ENVIRON = 24, 39
IS, SEND = 0, 1
VAR, VALUE, USERVAR = 0, 1, 3


def _ttype_is(name: str) -> bytes:
    """A client answering with its terminal type."""
    return bytes([IAC, SB, TTYPE, IS]) + name.encode() + bytes([IAC, SE])


def _environ_is(pairs: list[tuple[str, str]], *, user: bool = False) -> bytes:
    """A client answering with environment variables."""
    out = bytearray([IAC, SB, NEW_ENVIRON, IS])
    for key, value in pairs:
        out.append(USERVAR if user else VAR)
        out.extend(key.encode())
        out.append(VALUE)
        out.extend(value.encode())
    out.extend([IAC, SE])
    return bytes(out)


STREAMS: list[tuple[str, bytes]] = [
    ("nothing at all", b""),
    ("plain text", b"hello"),
    ("a lone IAC", bytes([IAC])),
    ("an escaped IAC", bytes([IAC, IAC])),
    ("text around an escaped IAC", b"a" + bytes([IAC, IAC]) + b"b"),
    ("an option this end wants", bytes([IAC, WILL, TTYPE])),
    ("an option this end refuses", bytes([IAC, WILL, 99])),
    ("an option offered the other way", bytes([IAC, DO, 99])),
    ("a refusal", bytes([IAC, WONT, TTYPE])),
    ("a refusal of something unknown", bytes([IAC, DONT, 99])),
    ("a terminal type", _ttype_is("xterm-256color")),
    ("a terminal type in capitals", _ttype_is("XTERM-256COLOR")),
    ("a plain terminal type", _ttype_is("vt100")),
    ("a terminal type of nothing", _ttype_is("")),
    ("an environment", _environ_is([("COLORTERM", "truecolor")])),
    ("an environment with several", _environ_is([("COLORTERM", "truecolor"), ("LANG", "en_GB")])),
    ("a user variable", _environ_is([("COLORTERM", "24bit")], user=True)),
    ("a terminal type then an environment", _ttype_is("xterm") + _environ_is([("COLORTERM", "truecolor")])),
    ("an environment then a terminal type", _environ_is([("COLORTERM", "truecolor")]) + _ttype_is("xterm")),
    ("text around a subnegotiation", b"before" + _ttype_is("xterm") + b"after"),
    ("a subnegotiation nobody handles", bytes([IAC, SB, 99, 1, 2, IAC, SE])),
    ("a subnegotiation that never ends", bytes([IAC, SB, TTYPE, IS]) + b"xterm"),
    (
        "an escaped IAC inside a subnegotiation",
        bytes([IAC, SB, TTYPE, IS]) + b"a" + bytes([IAC, IAC]) + b"b" + bytes([IAC, SE]),
    ),
    ("a terminal type sent twice", _ttype_is("xterm") + _ttype_is("vt100")),
    ("an empty subnegotiation", bytes([IAC, SB, TTYPE, IAC, SE])),
]

# term, env -> the colour mode the upstream session is opened with
COLOURS: list[tuple[str, str | None, dict[str, str]]] = [
    ("nothing known", None, {}),
    ("a true-colour hint", "xterm", {"COLORTERM": "truecolor"}),
    ("a 24-bit hint", "xterm", {"COLORTERM": "24bit"}),
    ("a hint in capitals", "xterm", {"COLORTERM": "TRUECOLOR"}),
    ("a hint nobody defined", "xterm", {"COLORTERM": "sideways"}),
    ("a 256-colour terminal", "xterm-256color", {}),
    ("a 256-colour terminal in capitals", "XTERM-256COLOR", {}),
    ("a plain terminal", "vt100", {}),
    ("a terminal with colour in its name", "xterm-color", {}),
    ("a screen", "screen", {}),
    ("a terminal of nothing", "", {}),
    ("a hint with no terminal", None, {"COLORTERM": "truecolor"}),
    ("both, disagreeing", "vt100", {"COLORTERM": "truecolor"}),
]


def _drive(name: str, stream: bytes, chunked: bool) -> dict[str, Any]:
    """Feed a stream to the real negotiator, whole or one byte at a time."""
    negotiator = iac.IacNegotiator()
    start = negotiator.start_bytes()
    replies = bytearray()
    cleaned = bytearray()
    pieces = [stream[index : index + 1] for index in range(len(stream))] if chunked else [stream]
    for piece in pieces:
        # `(reply, cleaned)`, in that order: what goes back to the client, then
        # what goes on upstream.
        reply, data = negotiator.feed(piece)
        replies.extend(reply)
        cleaned.extend(data)
    return {
        "name": name,
        "chunked": chunked,
        "stream": stream.decode("latin-1"),
        "start": start.decode("latin-1"),
        "cleaned": cleaned.decode("latin-1"),
        "reply": bytes(replies).decode("latin-1"),
        "term": negotiator.term,
        "env": dict(negotiator.env),
        "done": negotiator.done(),
        "colormode": negotiator.derived_colormode(),
    }


def main() -> None:
    corpus = {
        "start": iac.IacNegotiator().start_bytes().decode("latin-1"),
        "streams": [_drive(name, stream, chunked) for name, stream in STREAMS for chunked in (False, True)],
        "colours": [
            {"name": name, "term": term, "env": env, "colormode": iac.derive_colormode(term, env)}
            for name, term, env in COLOURS
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['streams'])} streams)")


if __name__ == "__main__":
    main()
