#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the deckmux parity golden from the REAL Python deckmux
(provide.uterm.deckmux). Run from the repo root:

    uv run python packages/provide-uterm-go/deckmux/testdata/gen_python_golden.py

Writes python_golden.json next to this script. The Go deckmux port must
reproduce every value here, so a divergence in either fails CI.

Two families carry most of the risk and neither is obvious from reading:

  * generate_name / generate_color / generate_initials are HASH-derived from a
    connection id, so any change to the hash, the wordlists, or the palette
    silently repoints every user's identity. The ids below are fixed so the
    derivation is checkable.
  * the identity frames are HMAC-signed over canonical JSON, so claim insertion
    order is part of the signed input. Each case records the secret it used;
    they are synthetic vectors, not credentials.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from provide.uterm.auth import ResolvedIdentity
from provide.uterm.control_channel_builders import make_identity
from provide.uterm.deckmux._identity import presence_from_identity
from provide.uterm.deckmux._names import generate_color, generate_initials, generate_name
from provide.uterm.deckmux._presence import UserPresence
from provide.uterm.deckmux._protocol import (
    encode_keys_display,
    make_control_transfer,
    make_presence_leave,
    make_presence_sync,
    make_presence_update,
)

# Arrow/control keys, both newline forms, both backspace forms, a bare ESC, a
# mixed run, unprintables that must vanish, and a truncated escape.
RAW_KEYS = [
    "\x1b[A",
    "\x1b[B",
    "\x1b[C",
    "\x1b[D",
    "\r",
    "\n",
    "\t",
    "\x7f",
    "\x08",
    "\x1b",
    "ls\r",
    "\x1b[Ahello\x1b[D",
    "\x01",
    "a\x02b",
    "",
    "\x1b[",
]

# Case-insensitivity, multi-word truncation to two, and the single-word case.
INITIALS_NAMES = ["Red Fox", "red fox", "Alice", "A B C", "A", "Storm Petrel"]

# Ids spanning the shapes real connections use: opaque, namespaced, service,
# and short ids that stress the palette's collision handling.
NAME_IDS = ["conn-123", "sre:alice", "svc-bot", "test-id", "id-a", "id-b", "probe-conn", "col-0"]

# subject/claims/role/connection_id -> the presence a joining identity becomes.
# Covers display_name vs display, empty claims, a bare subject, an empty role
# suffix, a claim-supplied colour, and an explicit role overriding a claim.
IDENTITY_INPUTS: list[dict[str, Any]] = [
    {
        "subject": "sre:alice",
        "claims": {"display_name": "Alice Liddell", "role": "oncall"},
        "role": "",
        "connection_id": "conn-1",
    },
    {"subject": "x", "claims": {"display": "Bob"}, "role": "", "connection_id": "conn-2"},
    {"subject": "sre:alice", "claims": {}, "role": "", "connection_id": "conn-3"},
    {"subject": "alice", "claims": {}, "role": "", "connection_id": "conn-4"},
    {"subject": "role:", "claims": {}, "role": "", "connection_id": "conn-5"},
    {"subject": "x", "claims": {"color": "#ff00aa"}, "role": "", "connection_id": "conn-6"},
    {"subject": "x", "claims": {"role": "admin"}, "role": "viewer", "connection_id": "c"},
    {"subject": "x", "claims": {}, "role": "viewer", "connection_id": "c"},
]

# (secret, make_identity kwargs). Unsigned-shaped claims matter: None omits the
# key entirely, {} keeps an empty object, and the two sign differently.
SIGNED: list[tuple[str, dict[str, Any]]] = [
    ("my-secret", {"subject": "x"}),
    (
        "top-secret",
        {
            "subject": "sre:alice",
            "claims": {"display_name": "Alice", "role": "oncall"},
            "fingerprint": "SHA256:abc",
        },
    ),
    ("s3", {"subject": "user:bob", "claims": {}, "transport": "ws"}),
]


def build() -> dict[str, Any]:
    sync_users = [
        UserPresence(user_id="sre:alice", name="Alice", color="#e74c3c", role="operator", initials="AL"),
        UserPresence(user_id="sre:bob", name="Bob", color="#3498db", role="viewer", initials="BO"),
    ]
    return {
        # A colour already taken must not be handed out twice.
        "color_taken": {
            "id": "test-id",
            "taken": ["#ff5722"],
            "color": generate_color("test-id", frozenset({"#ff5722"})),
        },
        "control_transfer_min": make_control_transfer("u1", "u2", "handover"),
        "control_transfer_queued": make_control_transfer("u1", "u2", "auto_idle", "ls\r"),
        "encode_keys": [{"raw": raw, "display": encode_keys_display(raw)} for raw in RAW_KEYS],
        "initials": [{"name": name, "initials": generate_initials(name)} for name in INITIALS_NAMES],
        "names": [
            {
                "id": conn_id,
                "name": generate_name(conn_id),
                "color": generate_color(conn_id),
                "initials": generate_initials(generate_name(conn_id)),
            }
            for conn_id in NAME_IDS
        ],
        "presence_from_identity": [
            {
                **case,
                "result": presence_from_identity(
                    ResolvedIdentity(subject=case["subject"], claims=case["claims"]),
                    case["connection_id"],
                    role=case["role"],
                ).to_dict(),
            }
            for case in IDENTITY_INPUTS
        ],
        "presence_leave": make_presence_leave("u1"),
        "presence_sync": make_presence_sync([{"user_id": "u1", "name": "Alice"}], {"idle_timeout": 30}),
        "presence_update_full": make_presence_update(
            "u1",
            "Alice",
            "#fff",
            "admin",
            scroll_line=42,
            scroll_range=[0, 100],
            selection={"start": 0},
            pin={"line": 5},
            typing=True,
            queued_keys="ls",
            is_owner=True,
        ),
        "presence_update_min": make_presence_update("u1", "Alice", "#fff", "admin"),
        "signed_identity": [
            {"secret": secret, "frame": make_identity(secret=secret, **kwargs)} for secret, kwargs in SIGNED
        ],
        "sync_payload": make_presence_sync(
            [user.to_dict() for user in sync_users],
            {"auto_transfer_idle_s": 30, "keystroke_queue": "display"},
        ),
        "to_dict_default": UserPresence(user_id="u1", name="Alice", color="#fff", role="admin").to_dict(),
        "to_dict_full": UserPresence(
            user_id="u1",
            name="Alice",
            color="#fff",
            role="admin",
            initials="AL",
            scroll_line=7,
            scroll_range=(3, 27),
            total_lines=99,
            selection={"start": {"row": 1, "col": 2}},
            pin={"row": 4, "col": 5},
            typing=True,
            queued_keys="ls↵",
            cols=132,
            rows=43,
            is_owner=True,
        ).to_dict(),
    }


def main() -> None:
    corpus = build()
    out = pathlib.Path(__file__).with_name("python_golden.json")
    # ensure_ascii=False: this corpus keeps its arrows and glyphs literal.
    out.write_text(
        json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out} ({len(corpus)} entries)")


if __name__ == "__main__":
    main()
