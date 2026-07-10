#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the cross-language conformance vectors from the Python reference.

Emits one JSON document (to stdout) whose every entry is an authoritative
input→output pair produced by the *Python* implementation. The Go conformance
test replays each input through the Go port and asserts the output matches
byte-for-byte. This proves Go and Python agree on every shared wire surface,
not merely that a frozen golden was copied.

Run standalone to refresh the committed golden:
    cd /Volumes/data/pyv/provide-uterm && \
      uv run python packages/provide-uterm-go/conformance/gen_vectors.py \
      > packages/provide-uterm-go/conformance/testdata/vectors.json
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys

sys.path.insert(0, "packages/provide-uterm/src")

from provide.uterm import ansi as _ansi
from provide.uterm import screen as _screen
from provide.uterm.control_channel import encode_control_frame, encode_terminal_data
from provide.uterm.control_channel_builders import make_identity
from provide.uterm.emulator import TerminalEmulator

# A deterministic corpus of payloads exercising the wire edge cases: escaped
# DLE, nested structures, unicode (BMP + astral), CP437 high bytes, empty.
_CONTROL_PAYLOADS = [
    {"type": "ping"},
    {"type": "hello", "worker_id": "w1", "capabilities": {"a": [1, 2, {"b": None}]}},
    {"type": "term", "data": "héllo → unicode 😀", "ts": 1234.5},
    {"type": "hijack_state", "hijacked": True, "owner": "op:alice", "lease_expires_at": 42.0},
    {"type": "identity", "subject": "user:bob", "claims": {"role": "oncall", "n": 3}},
]

_TERMINAL_DATA = [
    "plain text",
    "with \x10 escaped dle",
    "cp437: " + bytes(range(0xB0, 0xE0)).decode("latin-1"),
    "",
]

_SCREEN_TEXTS = [
    "\x1b[2J\x1b[HCommand [TL=00:00:00]:[3305] (?=Help)? : ",
    "1;31mRED at line start\nplain",
    "menu <A> Alpha <B> Beta   trailing",
    "├──┤ box \x1b[1;36mcyan\x1b[0m ~1tilde |07pipe {+g}brace{-x}",
]

_ANSI_TOKENS = [
    "{+r}red {-x}reset ~2white |04blue {F196}ext",
    "\x1b[31malready ansi\x1b[0m",
    "plain no tokens",
]

_HMAC_CASES = [
    ("secret-key-abc", "webhook body one"),
    ("another→secret", '{"json":true,"n":42}'),
    ("", "empty-secret-body"),  # must fail-closed on both sides
]

_IDENTITY_SIG_CASES = [
    ("user:alice", {"role": "oncall"}, "hmac-secret-1"),
    ("player:42", {"nested": {"a": [1, 2.5, "x"]}, "u": "→😀"}, "s2"),
    ("svc:bot", {}, "s3"),
]


def _webhook_signature(secret: str, body: str) -> str | None:
    if not secret:
        return None
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def main() -> None:
    emu = TerminalEmulator(40, 6)
    emu.process(b"\x1b[1;32mReady\x1b[0m\r\nprompt> ")
    snap = emu.get_snapshot()

    vectors = {
        "control_frames": [
            {"payload": p, "wire_b64": base64.b64encode(encode_control_frame(p).encode("utf-8")).decode()}
            for p in _CONTROL_PAYLOADS
        ],
        "terminal_data": [
            {"raw": d, "wire_b64": base64.b64encode(encode_terminal_data(d).encode("utf-8")).decode()}
            for d in _TERMINAL_DATA
        ],
        "normalize_terminal_text": [{"in": t, "out": _screen.normalize_terminal_text(t)} for t in _SCREEN_TEXTS],
        "cp437_roundtrip": [
            {"bytes_b64": base64.b64encode(bytes(v)).decode(), "decoded": _screen.decode_cp437(bytes(v))}
            for v in [list(range(256)), [0xC9, 0xCD, 0xBB]]
        ],
        "normalize_colors": [{"in": t, "out": _ansi.normalize_colors(t)} for t in _ANSI_TOKENS],
        "upgrade_256": [{"in": t, "out": _ansi.upgrade_to_256(t)} for t in _ANSI_TOKENS],
        "webhook_hmac": [{"secret": s, "body": b, "sig": _webhook_signature(s, b)} for s, b in _HMAC_CASES],
        "identity_signature": [
            {
                "subject": subj,
                "claims": claims,
                "secret": secret,
                "frame": make_identity(subj, claims=claims, secret=secret),
            }
            for subj, claims, secret in _IDENTITY_SIG_CASES
        ],
        "emulator_snapshot": {
            "feed_b64": base64.b64encode(b"\x1b[1;32mReady\x1b[0m\r\nprompt> ").decode(),
            "cols": 40,
            "rows": 6,
            "screen": snap["screen"],
            "screen_hash": snap["screen_hash"],
            "cursor": snap["cursor"],
            "cursor_at_end": snap["cursor_at_end"],
        },
    }
    json.dump(vectors, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
