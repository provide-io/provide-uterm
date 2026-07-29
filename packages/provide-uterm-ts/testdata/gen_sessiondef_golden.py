#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for config-backed session definitions.

A ``[[sessions]]`` entry is the one part of the config schema that does *not*
behave like the rest of it, and both differences are worth pinning:

* **A name nobody defined is kept, not refused.** Every other section forbids
  extras so a typo is a startup failure. Here a before-validator folds every
  unrecognised key into ``connector_config`` instead — which means a
  mistyped ``recording_enabled`` becomes a connector setting nothing reads,
  and recording stays off. The reference's behaviour, recorded rather than
  corrected.
* **The identifier is matched with CPython's ``\\w``, which is Unicode-aware.**
  A JavaScript ``\\w`` is ASCII-only, so a port that reads the pattern
  literally refuses identifiers the reference accepts. The corpus carries the
  non-ASCII cases that tell the two apart.

``created_at`` is left out of every case here: the reference parses a
date-time with Pydantic's own grammar, and reproducing that grammar's error
taxonomy is a unit of its own. The port checks the field is a string or a
number and leaves the parsing to whoever reads it — a recorded divergence,
not a silent one.

The rest is ordinary: the identifier is required and stripped, the display name
falls back to it, the connector type is checked against the registry, and the
input mode and visibility are closed sets whose refusals name the session.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_sessiondef_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from provide.uterm.server.config_schema import SERVER_BUILTIN_CONNECTOR_TYPES, SessionDefinition

OUT = Path(__file__).resolve().parent / "sessiondef_golden.json"


def _outcome(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Whether the reference accepts this entry, and what it says if not."""
    try:
        instance = SessionDefinition(**kwargs)
    except ValidationError as exc:
        return {
            "errors": [
                {"type": error["type"], "loc": list(error["loc"]), "msg": error["msg"]} for error in exc.errors()
            ]
        }
    accepted = json.loads(instance.model_dump_json())
    # Stamped at construction, so it would make this corpus differ from itself
    # on every run. Nothing here is about when a session was made.
    accepted.pop("created_at", None)
    return {"accepted": accepted}


CASES: list[tuple[str, dict[str, Any]]] = [
    ("the least an entry can say", {"session_id": "shell"}),
    ("no identifier at all", {}),
    ("an empty identifier", {"session_id": ""}),
    ("an identifier of only spaces", {"session_id": "   "}),
    ("an identifier with spaces around it", {"session_id": "  shell  "}),
    ("an identifier with a space in it", {"session_id": "my shell"}),
    ("an identifier with a dot in it", {"session_id": "my.shell"}),
    ("an identifier with a slash in it", {"session_id": "a/b"}),
    ("an identifier with dashes and underscores", {"session_id": "my-shell_2"}),
    ("an identifier of digits", {"session_id": "12345"}),
    # CPython's \w is Unicode-aware and JavaScript's is not, so these are the
    # cases that tell a literal reading of the pattern apart from a faithful one.
    ("an identifier in French", {"session_id": "café"}),
    ("an identifier in Japanese", {"session_id": "端末"}),
    ("an identifier in Cyrillic", {"session_id": "сессия"}),
    ("an identifier with an Arabic-Indic digit", {"session_id": "shell٣"}),
    ("an identifier with a combining accent", {"session_id": "café"}),
    ("an identifier with an emoji", {"session_id": "shell🐚"}),
    # Python's repr switches quote style rather than escaping, so these are
    # the cases that tell a faithful message apart from a plausible one.
    ("an identifier with an apostrophe", {"session_id": "a'b"}),
    ("an identifier with both kinds of quote", {"session_id": "a'b\"c"}),
    ("an identifier with a backslash", {"session_id": "a\\b"}),
    ("an identifier that is not a string", {"session_id": 5}),
    ("an identifier given null", {"session_id": None}),
    ("a display name of its own", {"session_id": "shell", "display_name": "Provide Shell"}),
    ("no display name", {"session_id": "shell"}),
    ("an empty display name", {"session_id": "shell", "display_name": ""}),
    ("a display name given null", {"session_id": "shell", "display_name": None}),
    ("an empty display name on an entry whose identifier is bad", {"session_id": "my shell", "display_name": ""}),
    ("a connector type the server has", {"session_id": "s", "connector_type": "ssh"}),
    ("a connector type nobody registered", {"session_id": "s", "connector_type": "carrier-pigeon"}),
    ("an empty connector type", {"session_id": "s", "connector_type": ""}),
    ("a connector type with spaces around it", {"session_id": "s", "connector_type": "  ssh  "}),
    ("the hijack input mode", {"session_id": "s", "input_mode": "hijack"}),
    ("an input mode nobody defined", {"session_id": "s", "input_mode": "readonly"}),
    ("an input mode given true", {"session_id": "s", "input_mode": True}),
    ("a visibility given false", {"session_id": "s", "visibility": False}),
    ("a visibility given a number", {"session_id": "s", "visibility": 3}),
    ("an input mode given null", {"session_id": "s", "input_mode": None}),
    ("an input mode on an entry with no identifier", {"input_mode": "readonly"}),
    ("a visibility the set has", {"session_id": "s", "visibility": "private"}),
    ("a visibility nobody defined", {"session_id": "s", "visibility": "everyone"}),
    ("a keystroke queue nobody defined", {"session_id": "s", "keystroke_queue": "drop"}),
    ("the replay keystroke queue", {"session_id": "s", "keystroke_queue": "replay"}),
    ("recording left unsaid", {"session_id": "s"}),
    ("recording turned on", {"session_id": "s", "recording_enabled": True}),
    ("recording given null", {"session_id": "s", "recording_enabled": None}),
    ("connector settings given outright", {"session_id": "s", "connector_config": {"host": "h", "port": 22}}),
    # The fold: what a section that forbids extras would have refused.
    ("a name nobody defined", {"session_id": "s", "host": "h.example"}),
    ("a mistyped field name", {"session_id": "s", "recordign_enabled": True}),
    (
        "a name nobody defined alongside connector settings",
        {"session_id": "s", "connector_config": {"host": "h"}, "port": 22},
    ),
    (
        "a name nobody defined that the settings already have",
        {"session_id": "s", "connector_config": {"host": "given"}, "host": "loose"},
    ),
    ("an idle transfer that is not a number", {"session_id": "s", "auto_transfer_idle_s": "soon"}),
    ("tags", {"session_id": "s", "tags": ["a", "b"]}),
    ("an owner", {"session_id": "s", "owner": "alice"}),
    ("an ephemeral session", {"session_id": "s", "ephemeral": True, "presence": True, "auto_start": False}),
]


# The connector type is only checked against the registry once something has
# registered — during startup it is empty, and refusing every type before the
# connectors load would make the server unable to start. These cases are
# recorded with a type registered, which is the other half of that rule.
REGISTERED_CASES: list[tuple[str, dict[str, Any]]] = [
    ("a connector type somebody registered", {"session_id": "s", "connector_type": "recorded-fake"}),
    ("a built-in connector type", {"session_id": "s", "connector_type": "ssh"}),
    ("a connector type nobody registered", {"session_id": "s", "connector_type": "carrier-pigeon"}),
    ("a connector type on an entry with no identifier", {"connector_type": "carrier-pigeon"}),
    ("an empty connector type", {"session_id": "s", "connector_type": "  "}),
]


def _with_registered_connector(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Record an outcome with the connector registry populated."""
    from provide.uterm.server.connectors import register_connector, registry

    # Two of them, one sorting before every built-in and one after, so the
    # list a refusal offers is pinned as sorted rather than as insertion
    # order.
    register_connector("recorded-fake", object)  # type: ignore[arg-type]
    register_connector("aaa-recorded", object)  # type: ignore[arg-type]
    try:
        return _outcome(kwargs)
    finally:
        registry._registry.pop("recorded-fake", None)
        registry._registry.pop("aaa-recorded", None)


def main() -> None:
    corpus = {
        "builtin_connector_types": sorted(SERVER_BUILTIN_CONNECTOR_TYPES),
        "cases": [{"name": name, "kwargs": kwargs, **_outcome(kwargs)} for name, kwargs in CASES],
        "registered_cases": [
            {"name": name, "kwargs": kwargs, **_with_registered_connector(kwargs)} for name, kwargs in REGISTERED_CASES
        ],
    }
    # NOT sorted: a definition's fields are recorded in the order the model
    # declares them, and that order is the order the reference reports errors
    # in.
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['cases'])} cases)")


if __name__ == "__main__":
    main()
