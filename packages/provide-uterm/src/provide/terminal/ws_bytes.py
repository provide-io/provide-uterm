#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Lossless byte ↔ str shim for the inline control channel.

The control channel (``provide.terminal.control_channel``) is a
str-typed protocol: ``ControlChannelDecoder.feed()`` takes ``str`` and
``DataChunk.data`` is ``str``. But the data carried inside it is raw
terminal bytes — typically CP437 from a BBS — and must not lose any
high bytes between the WebSocket boundary and the terminal emulator.

We use ``latin-1`` as the shim because it maps bytes 0x00-0xFF to
codepoints U+0000-U+00FF one-to-one with no replacements. CP437 is
*not* a valid shim here — cp437 has no codepoint for U+0080-U+009F,
so a latin-1→cp437 round-trip silently replaces every byte in that
range with ``?`` and destroys box-drawing characters.

CP437 decoding happens *exactly once*, inside the terminal emulator
(``TerminalEmulator.process``). Everything upstream stays byte-faithful.
"""

from __future__ import annotations


def ws_frame_to_channel_str(raw: str | bytes) -> str:
    """Coerce a WebSocket frame into the str form ``ControlChannelDecoder`` expects.

    Binary frames are decoded as latin-1 so every byte survives as a
    codepoint. Text frames are passed through (the sender is responsible
    for not emitting non-latin-1 codepoints into the channel).
    """
    if isinstance(raw, str):
        return raw
    return raw.decode("latin-1", errors="strict")


def channel_str_to_bytes(data: str) -> bytes:
    """Recover raw terminal bytes from a ``DataChunk.data`` string.

    Inverse of ``ws_frame_to_channel_str`` for the data segment. The
    result is the original byte stream that should be fed to a terminal
    emulator (which performs its own CP437 decode internally).
    """
    return data.encode("latin-1", errors="replace")
