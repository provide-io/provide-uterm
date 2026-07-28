#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``ctrlmsg`` port.

Covers the typed control-message builders and the link-pattern registry. The
identity builder's HMAC is the sharp edge: it signs a canonical-JSON payload,
so a byte of difference anywhere in claim serialisation produces a different
signature and a rejected identity.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_ctrlmsg_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.control_channel_builders import (
    make_identity,
    make_link_patterns,
    make_presence_update,
    make_resume,
    make_resume_failed,
    make_resume_ok,
    make_session_token,
)
from provide.uterm.control_channel_patterns import LinkPattern, LinkPatternRegistry

OUT = Path(__file__).with_name("ctrlmsg_golden.json")

# A fixed secret: these are synthetic test vectors, not a credential.
SECRET = "corpus-secret"  # noqa: S105

# (name, kwargs) for make_identity.
IDENTITY_CASES: list[tuple[str, dict[str, Any]]] = [
    ("subject only", {"subject": "user:alice"}),
    ("explicit empty fingerprint", {"subject": "u", "fingerprint": ""}),
    ("fingerprint and transport", {"subject": "u", "fingerprint": "SHA256:abc", "transport": "ssh"}),
    ("websocket transport", {"subject": "u", "transport": "websocket"}),
    ("empty claims mapping is kept", {"subject": "u", "claims": {}}),
    ("string claims", {"subject": "u", "claims": {"role": "operator"}}),
    ("multiple claims out of order", {"subject": "u", "claims": {"z": 1, "a": 2, "m": 3}}),
    ("integer and boolean claims", {"subject": "u", "claims": {"exp": 1735689600, "admin": True}}),
    ("nested claims", {"subject": "u", "claims": {"scopes": ["read", "write"], "meta": {"b": 1, "a": 2}}}),
    ("null claim", {"subject": "u", "claims": {"none": None}}),
    ("non-ascii claim", {"subject": "u", "claims": {"name": "José"}}),
    ("cjk claim", {"subject": "u", "claims": {"name": "你好"}}),
    ("non-ascii subject", {"subject": "user:José"}),
    # Signed variants: the same inputs plus a secret.
    ("signed, no claims", {"subject": "user:alice", "secret": SECRET}),
    ("signed, empty claims", {"subject": "u", "claims": {}, "secret": SECRET}),
    ("signed, string claims", {"subject": "u", "claims": {"role": "operator"}, "secret": SECRET}),
    ("signed, claims out of order", {"subject": "u", "claims": {"z": 1, "a": 2}, "secret": SECRET}),
    ("signed, nested claims", {"subject": "u", "claims": {"m": {"b": 1, "a": 2}}, "secret": SECRET}),
    ("signed, non-ascii claim", {"subject": "u", "claims": {"name": "José"}, "secret": SECRET}),
    ("signed, cjk claim", {"subject": "u", "claims": {"name": "你好"}, "secret": SECRET}),
    ("signed with fingerprint", {"subject": "u", "fingerprint": "SHA256:abc", "secret": SECRET}),
    ("signed with transport", {"subject": "u", "transport": "websocket", "secret": SECRET}),
    ("signed with bytes secret", {"subject": "u", "secret": SECRET.encode()}),
    # An empty secret is falsy, so no signature is added.
    ("empty secret leaves the frame unsigned", {"subject": "u", "secret": ""}),
]

# Link-pattern entries accepted by make_link_patterns.
LINK_PATTERN_CASES: list[tuple[str, list[dict[str, Any]]]] = [
    ("empty list", []),
    ("minimal entry", [{"pattern": r"\((\d+)\)", "action": "cmd"}]),
    ("every action", [{"pattern": "a", "action": a} for a in ("cmd", "url", "key", "focus")]),
    (
        "all optional fields",
        [
            {
                "pattern": "p",
                "action": "cmd",
                "id": "one",
                "flags": "gi",
                "group": 1,
                "payload": "$1",
                "hover": "go to $1",
                "class": "sector-link",
            }
        ],
    ),
    ("line_contains filter", [{"pattern": "p", "action": "cmd", "line_contains": "Sector"}]),
    # group is int | str | None, so a string group is accepted as written.
    ("string group", [{"pattern": "p", "action": "cmd", "group": "one"}]),
    # payload is Any, so a structured payload survives.
    ("structured payload", [{"pattern": "p", "action": "cmd", "payload": {"a": [1]}}]),
    # exclude_none drops an explicitly-null optional field.
    ("explicit null optionals", [{"pattern": "p", "action": "cmd", "id": None, "hover": None}]),
    ("explicit null payload", [{"pattern": "p", "action": "cmd", "payload": None}]),
    ("two entries", [{"pattern": "a", "action": "cmd"}, {"pattern": "b", "action": "url"}]),
]

# Entries make_link_patterns must refuse.
LINK_PATTERN_REJECTS: list[tuple[str, list[dict[str, Any]]]] = [
    ("missing pattern", [{"action": "cmd"}]),
    ("missing action", [{"pattern": "p"}]),
    ("invalid action", [{"pattern": "p", "action": "nope"}]),
    ("unknown field", [{"pattern": "p", "action": "cmd", "extra": 1}]),
    ("wrong type for pattern", [{"pattern": 1, "action": "cmd"}]),
    ("wrong type for group", [{"pattern": "p", "action": "cmd", "group": []}]),
    ("wrong type for flags", [{"pattern": "p", "action": "cmd", "flags": 1}]),
    ("second entry invalid", [{"pattern": "a", "action": "cmd"}, {"action": "cmd"}]),
]


def _identity_records() -> list[dict[str, Any]]:
    """Run every identity case and record the frame."""
    records = []
    for name, kwargs in IDENTITY_CASES:
        secret = kwargs.get("secret")
        records.append(
            {
                "name": name,
                "subject": kwargs["subject"],
                "claims": kwargs.get("claims"),
                "fingerprint": kwargs.get("fingerprint"),
                "transport": kwargs.get("transport"),
                "secret": secret.decode() if isinstance(secret, bytes) else secret,
                "frame": make_identity(**kwargs),
            }
        )
    return records


def _registry_record() -> dict[str, Any]:
    """Walk the registry through replace, unregister and clear."""
    registry = LinkPatternRegistry()
    steps: list[dict[str, Any]] = []

    def snapshot(label: str) -> None:
        steps.append({"step": label, "payload": registry.sync_payload()})

    snapshot("empty")
    registry.register(LinkPattern(pattern="a", action="cmd", id="one"))
    snapshot("one registered")
    registry.register(LinkPattern(pattern="b", action="url", id="two"))
    snapshot("two registered")
    # Re-registering an id replaces in place, preserving position.
    registry.register(LinkPattern(pattern="a2", action="key", id="one"))
    snapshot("first replaced in place")
    # Id-less patterns are appended and cannot be removed individually.
    registry.register(LinkPattern(pattern="c", action="focus"))
    registry.register(LinkPattern(pattern="d", action="focus"))
    snapshot("two anonymous appended")
    removed_known = registry.unregister("two")
    snapshot("known id removed")
    removed_unknown = registry.unregister("nope")
    snapshot("unknown id removal is a no-op")
    registry.clear()
    snapshot("cleared")
    # After clear the anonymous counter restarts.
    registry.register(LinkPattern(pattern="e", action="cmd"))
    snapshot("registered after clear")

    return {
        "steps": steps,
        "removed_known": removed_known,
        "removed_unknown": removed_unknown,
    }


def _pattern_entry_records() -> list[dict[str, Any]]:
    """to_frame_entry omits defaults and renames class_."""
    cases = [
        LinkPattern(pattern="p", action="cmd"),
        LinkPattern(pattern="p", action="url", id="x"),
        LinkPattern(pattern="p", action="cmd", flags="gi"),
        LinkPattern(pattern="p", action="cmd", flags="g"),
        LinkPattern(pattern="p", action="cmd", group=2),
        LinkPattern(pattern="p", action="cmd", group=0),
        LinkPattern(pattern="p", action="cmd", payload="$1"),
        LinkPattern(pattern="p", action="cmd", payload=""),
        LinkPattern(pattern="p", action="cmd", hover="h"),
        LinkPattern(pattern="p", action="cmd", class_="c"),
        LinkPattern(pattern="p", action="cmd", id="x", flags="i", group=1, payload="a", hover="b", class_="c"),
    ]
    return [{"entry": p.to_frame_entry()} for p in cases]


def main() -> int:
    """Write the golden corpus and report the record count."""
    link_pattern_records = []
    for name, entries in LINK_PATTERN_CASES:
        link_pattern_records.append({"name": name, "entries": entries, "frame": make_link_patterns(entries)})

    reject_records = []
    for name, entries in LINK_PATTERN_REJECTS:
        try:
            make_link_patterns(entries)
        except ValueError as exc:
            # The Pydantic detail text is version-specific; only the prefix is
            # part of the contract.
            reject_records.append({"name": name, "entries": entries, "error_prefix": str(exc).split(":")[0]})
        else:
            reject_records.append({"name": name, "entries": entries, "error_prefix": None})

    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_ctrlmsg_golden.py",
        "secret": SECRET,
        "identity": _identity_records(),
        "session_token": [
            {"token": "t", "player_id": None, "frame": make_session_token("t")},
            {"token": "t", "player_id": 7, "frame": make_session_token("t", 7)},
            {"token": "t", "player_id": 0, "frame": make_session_token("t", 0)},
        ],
        "resume": [
            {"token": "t", "player_id": None, "frame": make_resume("t")},
            {"token": "t", "player_id": 7, "frame": make_resume("t", 7)},
        ],
        "resume_ok": make_resume_ok(),
        "resume_failed": [
            {"reason": None, "frame": make_resume_failed()},
            {"reason": "expired", "frame": make_resume_failed("expired")},
            {"reason": "", "frame": make_resume_failed("")},
        ],
        "presence_update": [
            {"user_id": "u1", "fields": {}, "frame": make_presence_update("u1")},
            {"user_id": "u1", "fields": {"scroll_line": 5}, "frame": make_presence_update("u1", scroll_line=5)},
            {
                "user_id": "u1",
                "fields": {"cursor_row": 1, "cursor_col": 2},
                "frame": make_presence_update("u1", cursor_row=1, cursor_col=2),
            },
        ],
        "link_patterns": link_pattern_records,
        "link_pattern_rejects": reject_records,
        "pattern_entries": _pattern_entry_records(),
        "registry": _registry_record(),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    total = sum(len(v) for v in payload.values() if isinstance(v, list))
    print(f"wrote {OUT} ({total} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
