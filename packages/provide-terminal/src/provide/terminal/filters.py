#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Character-level input filters for BBS/telnet terminal sessions.

These async helpers consume and discard protocol-level byte sequences
(telnet IAC commands, ANSI escape sequences) from a byte-at-a-time
reader.  They are intended for interactive BBS sessions where arrow keys,
function keys, and telnet negotiation bytes must be silently discarded
rather than leaking into command input.

Usage::

    byte = (await reader.read(1))[0]
    if byte == IAC:
        await consume_iac(reader)
        continue
    if byte == ESC:
        await consume_escape(reader)
        continue
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Telnet IAC constants (RFC 854)
# ---------------------------------------------------------------------------
IAC: int = 255
WILL: int = 251
WONT: int = 252
DO: int = 253
DONT: int = 254
SB: int = 250
SE: int = 240

# ANSI escape
ESC: int = 0x1B


@runtime_checkable
class ByteReader(Protocol):
    """Minimal async reader — only ``read(n)`` is required."""

    async def read(self, n: int) -> bytes: ...


async def consume_iac(reader: ByteReader) -> None:
    """Consume and discard a telnet IAC command sequence.

    Called after the IAC byte (0xFF) has been read.  Handles:

    - Two-byte commands: WILL/WONT/DO/DONT + option byte
    - Sub-negotiation: SB ... IAC SE
    - IAC IAC (escaped 0xFF) — silently discarded
    """
    raw = await reader.read(1)
    if not raw:
        return
    cmd = raw[0]

    if cmd in (WILL, WONT, DO, DONT):
        await reader.read(1)  # option byte
    elif cmd == SB:
        while True:
            sb = await reader.read(1)
            if not sb:
                return
            if sb[0] == IAC:
                se = await reader.read(1)
                if not se or se[0] == SE:
                    return
    # IAC IAC or other — already consumed


async def consume_escape(reader: ByteReader) -> None:
    """Consume and discard an ANSI escape sequence.

    Called after the ESC byte (0x1B) has been read.  Handles:

    - CSI sequences: ESC ``[`` ... *final-byte*  (arrow keys, function keys)
    - SS3 sequences: ESC ``O`` *key*  (alternate cursor keys)
    - Two-char sequences: ESC *letter*  (Alt+key combos)
    """
    raw = await reader.read(1)
    if not raw:
        return
    b = raw[0]

    if b == 0x5B:  # '[' — CSI
        while True:
            raw = await reader.read(1)
            if not raw:
                return
            if 0x40 <= raw[0] <= 0x7E:
                return  # final byte
    elif b == 0x4F:  # 'O' — SS3
        await reader.read(1)
    # Otherwise ESC + single char — already consumed
