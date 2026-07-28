#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-server
"""Generate the differential golden corpus for the server's security response headers.

Which headers a response carries, given a mode and any per-header overrides.

**An empty override suppresses the header; an absent one takes the default.**
Those are different intentions and the config expresses them differently: a
deployment behind a proxy that already sets a policy has to be able to turn
one off without turning them all off, and `null` cannot mean both "leave it
alone" and "remove it".

**Dev mode keeps exactly one header.** Content sniffing is not a
development convenience — it is a bug in a browser that a page cannot work
around — so `nosniff` survives a mode that strips everything else.

**Only `strict` is strict.** The comparison is against that one name, so any
other value takes the relaxed set. A deployment that misspells the mode gets
the safer failure of a *visible* relaxation rather than a silent strictness
it did not ask for.

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_securityheaders_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.server.config_schema import SecurityConfig
from provide.uterm.server.security import _resolve_headers

OUT = Path(__file__).with_name("securityheaders_golden.json")


class _Config:
    """Just the fields the resolver reads, so a mode outside the schema can be tried."""

    def __init__(self, mode: str, **overrides: Any) -> None:
        self.mode = mode
        for field in (
            "csp",
            "hsts",
            "x_frame_options",
            "x_content_type_options",
            "referrer_policy",
            "permissions_policy",
        ):
            setattr(self, field, overrides.get(field))


# (name, mode, overrides) — what a response carries.
CASES: list[tuple[str, str, dict[str, Any]]] = [
    ("the strict default set", "strict", {}),
    ("the dev set", "dev", {}),
    # Any mode that is not `strict` takes the relaxed set.
    ("a mode nobody defined", "nonsense", {}),
    ("an empty mode", "", {}),
    ("a mode in capitals", "STRICT", {}),
    ("a mode with space around it", " strict ", {}),
    # An override replaces the default.
    ("a custom policy", "strict", {"csp": "default-src 'none'"}),
    ("a custom policy in dev mode", "dev", {"csp": "default-src 'none'"}),
    # An empty override suppresses the header.
    ("a suppressed policy", "strict", {"csp": ""}),
    ("a suppressed nosniff in dev mode", "dev", {"x_content_type_options": ""}),
    (
        "every header suppressed",
        "strict",
        {
            "csp": "",
            "hsts": "",
            "x_frame_options": "",
            "x_content_type_options": "",
            "referrer_policy": "",
            "permissions_policy": "",
        },
    ),
    # An override adds a header the mode's defaults do not carry.
    ("a policy added in dev mode", "dev", {"hsts": "max-age=1"}),
    ("several added in dev mode", "dev", {"hsts": "max-age=1", "x_frame_options": "SAMEORIGIN"}),
    (
        "every header overridden",
        "strict",
        {
            "csp": "a",
            "hsts": "b",
            "x_frame_options": "c",
            "x_content_type_options": "d",
            "referrer_policy": "e",
            "permissions_policy": "f",
        },
    ),
    # A value that is only whitespace is not empty, so it is used.
    ("a policy of one space", "strict", {"csp": " "}),
]


def _build() -> dict[str, Any]:
    """Everything the resolver decides."""
    return {
        # The schema's own default, so a port cannot pick a different one.
        "default_mode": SecurityConfig().mode,
        "header_order": [header for _field, header in _FIELDS],
        "resolved": [
            {
                "name": name,
                "mode": mode,
                "overrides": dict(overrides),
                "headers": [list(pair) for pair in _resolve_headers(_Config(mode, **overrides))],
            }
            for name, mode, overrides in CASES
        ],
        # A configuration straight from the schema, with nothing set.
        "schema_default": [list(pair) for pair in _resolve_headers(SecurityConfig())],
    }


_FIELDS = [
    ("csp", "Content-Security-Policy"),
    ("hsts", "Strict-Transport-Security"),
    ("x_frame_options", "X-Frame-Options"),
    ("x_content_type_options", "X-Content-Type-Options"),
    ("referrer_policy", "Referrer-Policy"),
    ("permissions_policy", "Permissions-Policy"),
]


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(CASES)} header sets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
