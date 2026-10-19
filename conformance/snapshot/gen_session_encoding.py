#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate ``session_encoding.json``, the session-decoding corpus.

Each record is the bytes a session's emulator is fed, read by read, the
session's receive encoding, and what the Python emulator holds afterwards: the
screen text, the cursor and the snapshot (``ansi_screen()``). Every port's
emulator, given the same encoding and the same reads, holds the same.

UTF-8 is decoded across reads, so a character split between two reads arrives
whole, and genuinely invalid bytes become one U+FFFD per maximal subpart.
CP437, a single-byte codec, maps every byte, including the ones UTF-8 would
start a sequence with. The reads are base64 because most are not text.

Generated once and committed; regenerate with::

    uv run python conformance/snapshot/gen_session_encoding.py
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from provide.uterm.emulator import TerminalEmulator

OUT = Path(__file__).with_name("session_encoding.json")

WIDE = "中文 café \U0001f600 é 中́"
"""Wide characters, an emoji, a precomposed and a combining accent, and a mark
joined to a wide character's second cell."""

#: (name, encoding, cols, rows, reads)
RECORDS: list[tuple[str, str, int, int, list[bytes]]] = [
    ("utf-8 wide characters and combining marks", "utf-8", 20, 3, [WIDE.encode()]),
    ("utf-8 split inside every multibyte character", "utf-8", 20, 3, [bytes([b]) for b in WIDE.encode()]),
    (
        "utf-8 split at every other byte",
        "utf-8",
        20,
        3,
        [WIDE.encode()[i : i + 2] for i in range(0, len(WIDE.encode()), 2)],
    ),
    ("utf-8 invalid bytes", "utf-8", 20, 3, [b"a\xffb\xe2\x82c\xed\xa0\x80d\xf0\x9f\x98", b"e\xc0\xaf"]),
    ("utf-8 a sequence the next read does not complete", "utf-8", 20, 3, [b"x\xe2\x82", b"y"]),
    ("utf-8 a split sequence then an escape", "utf-8", 20, 3, [b"\xe4\xb8", b"\xad\x1b[1;31m\xe6", b"\x96\x87"]),
    ("utf-8 dec graphics", "utf-8", 20, 3, [b"\x1b(0lqqk\x1b(B \x0eqq\x0f"]),
    ("cp437 high bytes", "cp437", 20, 3, [bytes(range(0xB0, 0xE0)), b"\r\n\x01\x02\x03"]),
    ("cp437 what utf-8 would decode", "cp437", 20, 3, ["中é".encode()]),
    ("cp437 ignores dec graphics", "cp437", 20, 3, [b"\x1b(0lqqk\x0eqq\x0f"]),
]


def main() -> int:
    """Write the corpus and report the record count."""
    records = []
    for name, encoding, cols, rows, reads in RECORDS:
        emulator = TerminalEmulator(cols, rows, receive_encoding=encoding)
        for read in reads:
            emulator.process(read)
        snapshot = emulator.get_snapshot()
        records.append(
            {
                "name": name,
                "encoding": encoding,
                "cols": cols,
                "rows": rows,
                "reads": [base64.b64encode(read).decode("ascii") for read in reads],
                "screen": snapshot["screen"],
                "cursor": snapshot["cursor"],
                "ansi_screen": emulator.ansi_screen(),
            }
        )
    payload = {"generator": "conformance/snapshot/gen_session_encoding.py", "records": records}
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(records)} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
