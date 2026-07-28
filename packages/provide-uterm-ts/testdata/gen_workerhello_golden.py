#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-server
"""Generate the differential golden corpus for reading a worker's protocol range.

The first frame a worker sends says which protocol versions it can speak, and
the hub has to read that from three shapes at once: the current one, a legacy
one, and nothing at all.

**A worker that advertises nothing speaks version one.** That is what every
client did before the field existed, and refusing them would disconnect every
worker built against an older hub.

**A legacy single version is a range of exactly itself.** It is not a minimum
with an open top: a worker that could only speak version one must not be
handed version two because the hub happened to support it.

**A version that is not a number falls back rather than failing.** The hello
carries the input mode too, and dropping the connection over an unreadable
version would lose that as well — the negotiation that follows is what decides
whether the two can talk.

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_workerhello_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.bridge.contracts import (
    MAX_PROTOCOL_VERSION,
    MIN_PROTOCOL_VERSION,
    negotiate_protocol_version,
)
from provide.uterm.server.bridge.models import _safe_int

OUT = Path(__file__).with_name("workerhello_golden.json")


def _client_range(msg: dict[str, Any]) -> tuple[int, int]:
    """The range a hello advertises, exactly as ``_handle_worker_hello`` reads it.

    Lifted verbatim from the reference's recv loop, which reads it inline
    rather than through a function of its own.
    """
    proto_block = msg.get("protocol")
    if isinstance(proto_block, dict):
        client_min = _safe_int(proto_block.get("min"), MIN_PROTOCOL_VERSION, min_val=1)
        client_max = _safe_int(proto_block.get("max"), MAX_PROTOCOL_VERSION, min_val=1)
    elif "protocol_version" in msg:
        legacy = _safe_int(msg.get("protocol_version"), 0)
        client_min = legacy if legacy >= 1 else 1
        client_max = client_min
    else:
        client_min = 1
        client_max = 1
    return client_min, client_max


# (name, hello) — what a worker advertises.
CASES: list[tuple[str, dict[str, Any]]] = [
    ("nothing at all", {}),
    ("only an input mode", {"input_mode": "open"}),
    # The current shape.
    ("a range", {"protocol": {"min": 1, "max": 1}}),
    ("a range with no minimum", {"protocol": {"max": 1}}),
    ("a range with no maximum", {"protocol": {"min": 1}}),
    ("an empty range object", {"protocol": {}}),
    ("a range asking for more than the hub has", {"protocol": {"min": 1, "max": 9}}),
    ("a range entirely above the hub", {"protocol": {"min": 5, "max": 9}}),
    ("a range of zero", {"protocol": {"min": 0, "max": 0}}),
    ("a negative range", {"protocol": {"min": -3, "max": -1}}),
    ("a range with a string version", {"protocol": {"min": "1", "max": "1"}}),
    ("a range with an unreadable version", {"protocol": {"min": "nonsense", "max": None}}),
    ("a range with a fractional version", {"protocol": {"min": 1.7, "max": 2.9}}),
    ("a range whose bounds are crossed", {"protocol": {"min": 9, "max": 1}}),
    # The legacy shape.
    ("a legacy version", {"protocol_version": 1}),
    ("a legacy version of zero", {"protocol_version": 0}),
    ("a legacy negative version", {"protocol_version": -2}),
    ("a legacy version above the hub", {"protocol_version": 7}),
    ("a legacy version that is null", {"protocol_version": None}),
    ("a legacy version that is a string", {"protocol_version": "2"}),
    ("a legacy version that is unreadable", {"protocol_version": "nonsense"}),
    # Both at once: the object wins, because it is the shape that can say a
    # range at all.
    ("both shapes", {"protocol": {"min": 1, "max": 1}, "protocol_version": 9}),
    # A protocol field that is not an object falls through to the legacy read.
    ("a protocol that is a number", {"protocol": 1}),
    ("a protocol that is a string", {"protocol": "1"}),
    ("a protocol that is a list", {"protocol": [1, 1]}),
    ("a protocol that is null", {"protocol": None}),
    ("a protocol that is not an object, with a legacy version", {"protocol": 1, "protocol_version": 1}),
]


def _build() -> dict[str, Any]:
    """What each hello advertises, and what it negotiates to."""
    out = []
    for name, hello in CASES:
        client_min, client_max = _client_range(hello)
        out.append(
            {
                "name": name,
                "hello": hello,
                "client_min": client_min,
                "client_max": client_max,
                "selected": negotiate_protocol_version(client_min, client_max),
            }
        )
    return {
        "server_min": MIN_PROTOCOL_VERSION,
        "server_max": MAX_PROTOCOL_VERSION,
        "hellos": out,
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(CASES)} hellos)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
