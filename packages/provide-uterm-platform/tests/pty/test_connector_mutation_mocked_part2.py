#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Fork-free mutation coverage for PTYConnector's UTF-8 decode + buffer-cap edges.

Companion to test_connector_mutation_mocked.py (split out for the 500-LOC gate).
These tests pin the codec/error-handler on the incremental decoder and the exact
buffer-truncation threshold — the surfaces whose ``# pragma: no mutate`` markers
were removed (L7 mutation-hygiene). Each drives poll_messages over a real
``os.pipe()`` with no child process, so they run under mutmut without forking.
"""

from __future__ import annotations

import os
from typing import Any

from provide.uterm.pty.connector import PTYConnector


def _conn(**config: Any) -> PTYConnector:
    cfg = {"command": "/bin/echo", "args": []}
    cfg.update(config)
    return PTYConnector(session_id="s1", display_name="d", config=cfg)


def _connected(conn: PTYConnector, master_fd: int) -> None:
    conn._master_fd = master_fd
    conn._connected = True


async def test_poll_decodes_invalid_utf8_to_replacement_char() -> None:
    """An invalid UTF-8 byte decodes to U+FFFD via the ``errors='replace'`` handler.

    Pins both the codec name and the error handler on the incremental decoder:
    a mutant that swaps the handler to an unknown name ('XXreplaceXX'/'REPLACE')
    raises LookupError, and one that drops it to None raises UnicodeDecodeError —
    either way poll_messages raises here instead of yielding the replacement char.
    (The codec-name case flip 'utf-8'->'UTF-8' is a documented equivalent: the
    registry lookup is case-insensitive, so it cannot be distinguished.)
    """
    r, w = os.pipe()
    try:
        conn = _conn()
        _connected(conn, r)
        os.write(w, b"\xff")  # 0xff is never a valid UTF-8 start byte
        out = await conn.poll_messages()
        assert conn._buffer == "�"
        assert out and out[0]["screen"] == "�"
    finally:
        os.close(w)
        os.close(r)


async def test_poll_truncates_buffer_at_exactly_one_over_cap() -> None:
    """A buffer exactly one char over the 32768 cap is truncated back to 32768.

    Pins the threshold literal against an off-by-one: a ``> 32769`` mutant leaves
    the buffer at 32769 (32769 is not > 32769), so asserting the post-truncation
    length is exactly 32768 kills it. (The ``>``->``>=`` mutant is a documented
    equivalent: at the boundary the ``[-32768:]`` slice is a no-op.)
    """
    r, w = os.pipe()
    try:
        conn = _conn()
        _connected(conn, r)
        conn._buffer = "x" * 32767
        os.write(w, b"yy")  # 32767 + 2 = 32769 > 32768 → truncated back to 32768
        await conn.poll_messages()
        assert len(conn._buffer) == 32768
        assert conn._buffer.endswith("yy")
    finally:
        os.close(w)
        os.close(r)
