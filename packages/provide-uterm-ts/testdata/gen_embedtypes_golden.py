#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the embed layer's types.

Which attached clients a broadcast reaches, and what an embedded session
answers a telnet server with.

**An exclusion beats a requirement.** A client carrying both a required tag
and an excluded one is excluded, because the exclusion is the narrower
statement — an operator adding one means "not these", and a requirement that
overrode it would make the exclusion silently useless.

**An absent filter is no constraint, not an empty one.** A filter naming no
required tags matches every client rather than none, or attaching a filter to
say one thing would stop every broadcast.

**Telnet negotiation is answered symmetrically.** A DO is met with a WILL and a
WILL with a DO — the policy accepts whatever is offered — and a WONT or DONT is
mirrored back so neither end waits on the other.

**Only two subnegotiations are answered.** A terminal-type request and a window
size; anything else gets nothing rather than a guess, because a wrong answer to
an option nobody implemented is worse than silence.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_embedtypes_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.embed.types import (
    BackpressurePolicy,
    ClientFilter,
    ClientMetadata,
    DefaultTelnetPolicy,
    InterceptAction,
    InterceptResult,
)

OUT = Path(__file__).with_name("embedtypes_golden.json")

# (name, tags, require_any, exclude) — which clients a broadcast reaches.
FILTER_CASES: list[tuple[str, list[str], list[str] | None, list[str] | None]] = [
    ("no filter at all", ["a"], None, None),
    ("a required tag the client has", ["a", "b"], ["a"], None),
    ("a required tag the client lacks", ["b"], ["a"], None),
    ("any of several required tags", ["b"], ["a", "b"], None),
    ("none of several required tags", ["c"], ["a", "b"], None),
    ("an excluded tag the client has", ["a"], None, ["a"]),
    ("an excluded tag the client lacks", ["b"], None, ["a"]),
    # The exclusion is the narrower statement and wins.
    ("both required and excluded", ["a"], ["a"], ["a"]),
    ("required, and excluded by another", ["a", "b"], ["a"], ["b"]),
    # An empty list is no constraint, not an impossible one.
    ("an empty requirement", ["a"], [], None),
    ("an empty exclusion", ["a"], None, []),
    ("a client with no tags", [], ["a"], None),
    ("a client with no tags and no filter", [], None, None),
]

# (name, command, option) — what the policy answers a negotiation with.
OPTION_CASES: list[tuple[str, int, int]] = [
    ("a DO", 253, 24),
    ("a WILL", 251, 1),
    ("a WONT", 252, 1),
    ("a DONT", 254, 24),
    ("a command nobody sends", 200, 24),
    ("a DO for a high option", 253, 255),
    ("a DO for option zero", 253, 0),
]

# (name, option, body) — what a subnegotiation is answered with.
SUBNEG_CASES: list[tuple[str, int, bytes]] = [
    ("a terminal-type request", 24, bytes((1,))),
    ("a terminal-type request with a trailing byte", 24, bytes((1, 2))),
    ("a terminal type that is not a request", 24, bytes((0,))),
    ("a terminal-type request with no body", 24, b""),
    ("a window-size request", 31, b""),
    ("a window-size request with a body", 31, bytes((1, 2))),
    ("an option nobody implemented", 99, bytes((1,))),
]


def _build() -> dict[str, Any]:
    """Everything the embed types decide."""
    policy = DefaultTelnetPolicy()
    wide = DefaultTelnetPolicy(terminal_type="xterm-256color", window_size=(1000, 300))
    return {
        "default_terminal_type": policy.terminal_type,
        "default_window_size": list(policy.window_size),
        "default_backpressure": BackpressurePolicy.DROP_OLDEST.name,
        "default_queue_capacity": ClientMetadata(client_id="c").queue_capacity,
        "intercept_actions": [action.name for action in InterceptAction],
        "intercept_defaults": {
            "pass": [InterceptResult.pass_().action.name, None],
            "replace": [InterceptResult.replace(b"xy").action.name, list(b"xy")],
            "consume": [InterceptResult.consume().action.name, None],
            "defer": [InterceptResult.defer().action.name, None],
            "inject": [InterceptResult.inject(b"z").action.name, list(b"z")],
        },
        "filters": [
            {
                "name": name,
                "tags": tags,
                "require_any_tag": require,
                "exclude_tags": exclude,
                "result": ClientFilter(require_any_tag=require, exclude_tags=exclude).matches(
                    ClientMetadata(client_id="c", tags=set(tags))
                ),
            }
            for name, tags, require, exclude in FILTER_CASES
        ],
        "options": [
            {"name": name, "command": command, "option": option, "reply": list(policy.on_option(command, option))}
            for name, command, option in OPTION_CASES
        ],
        "subnegotiations": [
            {
                "name": name,
                "option": option,
                "body": list(body),
                "reply": list(policy.on_subnegotiation(option, body)),
                "wide_reply": list(wide.on_subnegotiation(option, body)),
            }
            for name, option, body in SUBNEG_CASES
        ],
        # A terminal type that will not encode, and a window size that needs
        # both bytes.
        "unencodable_terminal_type": list(
            DefaultTelnetPolicy(terminal_type="xterm-✓").on_subnegotiation(24, bytes((1,)))
        ),
        "wide_window": list(wide.on_subnegotiation(31, b"")),
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(FILTER_CASES)} filters, {len(OPTION_CASES)} options)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
