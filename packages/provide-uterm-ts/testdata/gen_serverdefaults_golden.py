#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-server
"""Generate the differential golden corpus for the server configuration's defaults.

What a server runs as when its config file says nothing. The TOML loader
merges a document over these, so every value a deployment does not set comes
from here — which makes them the actual default posture rather than a
description of one.

Recorded from the schema itself rather than transcribed, so a default that
changes on the Python side fails the drift check instead of quietly leaving
the two ports running differently.

Volatile fields are dropped: a session's creation time is stamped when the
model is built, and recording it would make this corpus differ from itself on
every run.

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_serverdefaults_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.server.config_schema import UtermServerConfig

OUT = Path(__file__).with_name("serverdefaults_golden.json")

#: Stamped at construction, so recording it would make the corpus move.
VOLATILE_FIELDS: frozenset[str] = frozenset({"created_at"})


def _strip(value: Any) -> Any:
    """Drop the fields that change between runs."""
    if isinstance(value, dict):
        return {key: _strip(inner) for key, inner in value.items() if key not in VOLATILE_FIELDS}
    if isinstance(value, list):
        return [_strip(item) for item in value]
    return value


def _build() -> dict[str, Any]:
    """The whole default configuration, and its shape."""
    config = UtermServerConfig()
    dumped = _strip(config.model_dump(mode="json"))
    return {
        "config": dumped,
        # The sections a document may carry, which the loader's structural pass
        # already checks are tables.
        "sections": sorted(key for key, value in dumped.items() if isinstance(value, dict)),
        # And the scalars that sit at the top level beside them.
        "top_level_scalars": sorted(key for key, value in dumped.items() if not isinstance(value, (dict, list))),
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['sections'])} sections)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
