#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``filters`` port.

Each record drives the CPython reference consumer over a byte stream and
records what is left unread. That residue is the whole observable contract:
the consumers return nothing, so "how far did it read" is the only behaviour
a caller can depend on.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_filters_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from provide.uterm.filters import DO, DONT, ESC, IAC, SB, SE, WILL, WONT, consume_escape, consume_iac

OUT = Path(__file__).with_name("filters_golden.json")


class _Reader:
    """Byte-at-a-time reader over a fixed buffer, mirroring the ByteReader protocol."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.pos = 0

    async def read(self, n: int) -> bytes:
        chunk = self._data[self.pos : self.pos + n]
        self.pos += len(chunk)
        return chunk


# (name, consumer, stream). The stream is what follows the introducer byte,
# which the caller has already read.
IAC_CASES: list[tuple[str, bytes]] = [
    ("empty stream after IAC", b""),
    ("WILL + option", bytes([WILL, 1]) + b"rest"),
    ("WONT + option", bytes([WONT, 1]) + b"rest"),
    ("DO + option", bytes([DO, 1]) + b"rest"),
    ("DONT + option", bytes([DONT, 1]) + b"rest"),
    ("WILL with the option byte missing", bytes([WILL])),
    ("escaped IAC IAC", bytes([IAC]) + b"rest"),
    ("unknown command byte", bytes([0x01]) + b"rest"),
    ("subnegotiation terminated by IAC SE", bytes([SB, 0x18, 0x00, IAC, SE]) + b"rest"),
    ("subnegotiation with an escaped IAC inside", bytes([SB, 0x18, IAC, IAC, 0x00, IAC, SE]) + b"rest"),
    ("subnegotiation truncated before IAC", bytes([SB, 0x18, 0x00])),
    ("subnegotiation truncated after IAC", bytes([SB, 0x18, IAC])),
    ("subnegotiation with an empty payload", bytes([SB, IAC, SE]) + b"rest"),
    # IAC followed by a byte that is not SE keeps the scan going: the guard
    # returns only on SE or on exhaustion, so the next IAC SE terminates it.
    ("subnegotiation with IAC then a non-SE byte", bytes([SB, 0x18, IAC, 0x00, IAC, SE]) + b"rest"),
]

ESC_CASES: list[tuple[str, bytes]] = [
    ("empty stream after ESC", b""),
    ("CSI cursor up", b"[A" + b"rest"),
    ("CSI with parameters", b"[1;2H" + b"rest"),
    ("CSI with a final byte at the low end of the range", b"[@" + b"rest"),
    ("CSI with a final byte at the high end of the range", b"[~" + b"rest"),
    ("CSI with a byte just below the final-byte range", b"[?" + b"1049h" + b"rest"),
    ("CSI truncated before the final byte", b"[1;2"),
    ("CSI with nothing after the bracket", b"["),
    ("SS3 cursor key", b"OP" + b"rest"),
    ("SS3 with the key byte missing", b"O"),
    ("two-character alt combo", b"a" + b"rest"),
    ("ESC ESC", bytes([ESC]) + b"rest"),
]


async def _run() -> dict[str, list[dict[str, object]]]:
    """Drive both consumers over every case and record the unread residue."""
    iac_records: list[dict[str, object]] = []
    for name, stream in IAC_CASES:
        reader = _Reader(stream)
        await consume_iac(reader)
        iac_records.append(
            {"name": name, "stream": stream.hex(), "consumed": reader.pos, "remaining": stream[reader.pos :].hex()}
        )
    esc_records: list[dict[str, object]] = []
    for name, stream in ESC_CASES:
        reader = _Reader(stream)
        await consume_escape(reader)
        esc_records.append(
            {"name": name, "stream": stream.hex(), "consumed": reader.pos, "remaining": stream[reader.pos :].hex()}
        )
    return {"iac": iac_records, "escape": esc_records}


def main() -> int:
    """Write the golden corpus and report the record count."""
    sections = asyncio.run(_run())
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_filters_golden.py",
        "constants": {
            "IAC": IAC,
            "WILL": WILL,
            "WONT": WONT,
            "DO": DO,
            "DONT": DONT,
            "SB": SB,
            "SE": SE,
            "ESC": ESC,
        },
        **sections,
    }
    OUT.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    total = len(sections["iac"]) + len(sections["escape"])
    print(f"wrote {OUT} ({total} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
