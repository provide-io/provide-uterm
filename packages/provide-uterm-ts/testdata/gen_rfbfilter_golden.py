#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the RFB client-input filter.

What a viewer is allowed to send a graphical session. Everything that only
*reads* the screen passes through; the three messages that act on it —
keystrokes, pointer movement and clipboard writes — are gated.

**A missing permission check fails closed.** No callback means no injection,
because the alternative is a relay that forwards keystrokes when whoever wired
it up forgot to say who may send them.

**A refused message is dropped, not refused.** The stream is a byte protocol
with no room for an error, so a keystroke a viewer may not send is silently
not forwarded — the session stays up and the viewer sees nothing happen.

**An oversized clipboard write is drained and dropped.** Raising would tear
down the relay and black the framebuffer for everyone watching, which is a
worse answer to one hostile message than ignoring it.

**The clipboard length has its high bit stripped.** RFB's extended clipboard
sets it and the remaining thirty-one bits are the real size; reading the field
whole would make every extended write look like two gigabytes and be dropped.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_rfbfilter_golden.py
"""

from __future__ import annotations

import io
import json
import struct
from pathlib import Path
from typing import Any

from provide.uterm.vnc.rfb_filter import MAX_CUT_TEXT, filter_rfb_client_input

OUT = Path(__file__).with_name("rfbfilter_golden.json")

SET_PIXEL_FORMAT, SET_ENCODINGS, UPDATE_REQUEST, KEY_EVENT, POINTER_EVENT, CUT_TEXT = 0, 2, 3, 4, 5, 6

#: The handshake every stream opens with: version, security type, ClientInit.
HANDSHAKE = b"RFB 003.008\n" + bytes((1,)) + bytes((1,))


def _cut_text(payload: bytes, *, extended: bool = False) -> bytes:
    """A ClientCutText message carrying *payload*."""
    length = len(payload) | (0x80000000 if extended else 0)
    return bytes((CUT_TEXT,)) + b"\0\0\0" + struct.pack("!I", length) + payload


#: (name, body after the handshake) — one stream each.
STREAM_CASES: list[tuple[str, bytes]] = [
    ("the handshake alone", b""),
    ("a pixel format", bytes((SET_PIXEL_FORMAT,)) + bytes(19)),
    ("no encodings", bytes((SET_ENCODINGS,)) + b"\0" + struct.pack("!H", 0)),
    ("two encodings", bytes((SET_ENCODINGS,)) + b"\0" + struct.pack("!H", 2) + bytes(8)),
    ("an update request", bytes((UPDATE_REQUEST,)) + bytes(9)),
    ("two update requests", (bytes((UPDATE_REQUEST,)) + bytes(9)) * 2),
    ("a keystroke", bytes((KEY_EVENT,)) + bytes(7)),
    ("pointer movement", bytes((POINTER_EVENT,)) + bytes(5)),
    ("a clipboard write", _cut_text(b"hello")),
    ("an empty clipboard write", _cut_text(b"")),
    ("an extended clipboard write", _cut_text(b"hello", extended=True)),
    # Reading and acting, interleaved.
    (
        "a viewer doing everything",
        bytes((SET_PIXEL_FORMAT,))
        + bytes(19)
        + bytes((UPDATE_REQUEST,))
        + bytes(9)
        + bytes((KEY_EVENT,))
        + bytes(7)
        + bytes((POINTER_EVENT,))
        + bytes(5)
        + _cut_text(b"hi")
        + bytes((UPDATE_REQUEST,))
        + bytes(9),
    ),
]


def _run(body: bytes, *, allow: bool | None = True, handshake: bytes = HANDSHAKE) -> dict[str, Any]:
    """Run one stream through the filter and record what came out."""
    src = io.BytesIO(handshake + body)
    dst = io.BytesIO()
    ready: list[int] = []
    can_inject = None if allow is None else (lambda *_args, _a=allow: _a)
    try:
        filter_rfb_client_input(
            dst,
            src,
            can_inject=can_inject,
            session_id="s",
            lease_id="l",
            principal_id="p",
            principal_role="operator",
            on_client_ready=lambda: ready.append(1),
        )
        error = None
    except (ValueError, EOFError) as exc:
        error = type(exc).__name__
    return {"out": list(dst.getvalue()), "ready": len(ready), "error": error}


def _build() -> dict[str, Any]:
    """Everything the filter decides."""
    oversized = bytes((CUT_TEXT,)) + b"\0\0\0" + struct.pack("!I", MAX_CUT_TEXT + 1) + bytes(64)
    return {
        "handshake": list(HANDSHAKE),
        "max_cut_text": MAX_CUT_TEXT,
        "streams": [
            {
                "name": name,
                "body": list(body),
                "allowed": _run(body, allow=True),
                "refused": _run(body, allow=False),
                "no_checker": _run(body, allow=None),
            }
            for name, body in STREAM_CASES
        ],
        # A security type the filter does not implement.
        "bad_security_type": _run(b"", handshake=b"RFB 003.008\n" + bytes((2,)) + bytes((1,))),
        # A message type nobody sends.
        "unknown_message": _run(bytes((99,)), allow=True),
        # A stream that stops mid-message.
        "short_message": _run(bytes((KEY_EVENT,)) + bytes(3), allow=True),
        "short_handshake": _run(b"", handshake=b"RFB 003."),
        # A clipboard write larger than the cap: drained and dropped, and the
        # stream survives.
        "oversized_clipboard": _run(oversized, allow=True),
        "oversized_then_more": _run(oversized + bytes((UPDATE_REQUEST,)) + bytes(9), allow=True),
        # A client that sends every byte it declared: the drain completes and
        # the session carries on, which is what the cap is for.
        "oversized_fully_sent": _run(
            bytes((CUT_TEXT,))
            + b"\0\0\0"
            + struct.pack("!I", MAX_CUT_TEXT + 1)
            + bytes(MAX_CUT_TEXT + 1)
            + bytes((UPDATE_REQUEST,))
            + bytes(9),
            allow=True,
        ),
        # And one exactly at the cap, which is forwarded rather than drained.
        "clipboard_at_the_cap": _run(
            bytes((CUT_TEXT,)) + b"\0\0\0" + struct.pack("!I", MAX_CUT_TEXT) + bytes(MAX_CUT_TEXT),
            allow=True,
        ),
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(STREAM_CASES)} streams)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
