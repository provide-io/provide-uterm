#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-server
"""Generate the differential golden corpus for loading the server's TOML config.

What happens between a file on disk and a validated configuration: merging a
document over the defaults, refusing a section that is not a table, and
resolving a recording directory written relative to the file.

**A section written as something other than a table is refused by name.** TOML
lets a key hold a string where a table was meant, and the resulting error from
a schema is about a field nobody wrote — naming the section says which line to
look at.

**The merge is deep, and only where both sides are tables.** A partial
``[auth]`` section must leave the rest of the defaults standing; a list must
replace rather than merge, because half of one list and half of another is not
a configuration anybody wrote.

**A session entry that is not a table is dropped, not refused.** The list is
the one place the reference is lenient, and it is lenient deliberately: one bad
entry should not stop a server that has other sessions to serve.

**A relative recording directory is resolved against the file, not the working
directory.** A config is read from wherever it lives and a server is started
from wherever the operator happens to be; resolving against the process would
put recordings somewhere neither of them chose.

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_configloader_golden.py
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from provide.uterm.server.config import _deep_merge, config_from_mapping

_DATETIME = datetime.datetime(1979, 5, 27, 7, 32, tzinfo=datetime.UTC)
_DATE = datetime.date(1979, 5, 27)
_TIME = datetime.time(7, 32)

OUT = Path(__file__).with_name("configloader_golden.json")


def _error(call: Any) -> dict[str, Any]:
    """What a load refuses, and how it says so."""
    try:
        call()
    except (ValueError, TypeError) as exc:
        return {"error": type(exc).__name__, "message": str(exc)}
    return {"error": None, "message": None}


# (name, base, override) — how two documents combine.
MERGE_CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("nothing over nothing", {}, {}),
    ("a value over nothing", {}, {"a": 1}),
    ("nothing over a value", {"a": 1}, {}),
    ("a value replaced", {"a": 1}, {"a": 2}),
    ("a value of another type", {"a": 1}, {"a": "two"}),
    ("a table merged", {"t": {"a": 1, "b": 2}}, {"t": {"b": 3}}),
    ("a table added", {"t": {"a": 1}}, {"u": {"b": 2}}),
    ("a nested table merged", {"t": {"u": {"a": 1, "b": 2}}}, {"t": {"u": {"b": 3}}}),
    # A table replacing a scalar, and a scalar replacing a table: neither side
    # can be merged, so the override simply wins.
    ("a table over a value", {"a": 1}, {"a": {"b": 2}}),
    ("a value over a table", {"a": {"b": 2}}, {"a": 1}),
    # A string base: only meaningful in a language whose strings are
    # indexable, where merging *into* one would produce a table of its
    # characters.
    ("a table over a string", {"a": "abc"}, {"a": {"b": 2}}),
    # A list replaces rather than merges: half of one and half of another is
    # not a configuration anybody wrote.
    ("a list replaced", {"a": [1, 2, 3]}, {"a": [4]}),
    ("an empty list", {"a": [1, 2]}, {"a": []}),
    ("a null over a value", {"a": 1}, {"a": None}),
    ("a table over a null", {"a": None}, {"a": {"b": 1}}),
]

# (name, document) — what a mapping is allowed to say.
MAPPING_CASES: list[tuple[str, dict[str, Any]]] = [
    ("an empty document", {}),
    ("a partial section", {"auth": {"mode": "jwt"}}),
    ("an unknown top-level key", {"nonsense": 1}),
    # Every section that must be a table, each written as something else.
    ("server as a string", {"server": "nope"}),
    ("auth as a string", {"auth": "nope"}),
    ("ui as a number", {"ui": 7}),
    ("recording as a list", {"recording": []}),
    ("profiles as a boolean", {"profiles": True}),
    ("security as a string", {"security": "x"}),
    ("tunnel as a number", {"tunnel": 1}),
    ("webhooks as a string", {"webhooks": "x"}),
    ("pam as a list", {"pam": [1]}),
    ("control_plane as a string", {"control_plane": "x"}),
    ("a section that is null", {"auth": None}),
    # TOML has a datetime type, and a parser hands it back as a native date
    # object — which in a language whose dates are objects would read as a
    # table unless it is refused by name.
    ("a section that is a datetime", {"auth": _DATETIME}),
    ("a section that is a date", {"auth": _DATE}),
    ("a section that is a time", {"auth": _TIME}),
    # Sessions is a list, and its entries are filtered rather than refused.
    ("sessions as a list of tables", {"sessions": [{"name": "a"}, {"name": "b"}]}),
    ("sessions with a bad entry", {"sessions": [{"name": "a"}, "nope", 7, None]}),
    ("sessions entirely bad", {"sessions": ["nope"]}),
    ("sessions as an empty list", {"sessions": []}),
    ("sessions as a table", {"sessions": {"name": "a"}}),
    ("sessions as a string", {"sessions": "nope"}),
]


def _mapping_outcome(document: dict[str, Any]) -> dict[str, Any]:
    """What a document becomes, or why it is refused.

    ``kind`` says which layer refused it. The structural pass — the table
    check and the session filter — runs before the schema and is what a port
    can reproduce exactly; a schema refusal depends on the whole 534-line
    model and is recorded as having happened, not by its wording.
    """
    outcome = _error(lambda: config_from_mapping(dict(document)))
    if outcome["error"] is not None:
        structural = outcome["message"].startswith("[")
        return {
            "kind": "structural" if structural else "schema",
            "error": outcome["error"],
            "message": outcome["message"] if structural else None,
            "sessions": None,
        }
    config = config_from_mapping(dict(document))
    return {
        "kind": "accepted",
        "error": None,
        "message": None,
        # ``created_at`` is stamped at construction, so recording it would
        # make this corpus differ from itself on every run.
        "sessions": [
            {key: value for key, value in entry.model_dump(mode="json").items() if key != "created_at"}
            for entry in config.sessions
        ],
    }


def _build() -> dict[str, Any]:
    """Everything loading decides."""
    return {
        "table_sections": sorted(
            ["server", "auth", "ui", "recording", "profiles", "security", "tunnel", "webhooks", "pam", "control_plane"]
        ),
        "merges": [
            {"name": name, "base": base, "override": override, "result": _deep_merge(base, override)}
            for name, base, override in MERGE_CASES
        ],
        "mappings": [
            {"name": name, "document": document, **_mapping_outcome(document)} for name, document in MAPPING_CASES
        ],
        # The merge does not touch what it was given.
        "merge_is_pure": _merge_purity(),
    }


def _merge_purity() -> dict[str, Any]:
    """A merge must not mutate either side."""
    base = {"t": {"a": 1}}
    override = {"t": {"b": 2}}
    _deep_merge(base, override)
    return {"base": base, "override": override}


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(
        json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT} ({len(MERGE_CASES)} merges, {len(MAPPING_CASES)} mappings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
