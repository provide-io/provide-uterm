#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the hijack client's guards.

The client turns caller-supplied identifiers into request paths and writes
what it saw into a log, so two things have to hold before anything reaches a
server or a logfile:

* **An identifier is one path segment or it is refused.** A worker id holding
  a slash, a dot-dot, or a query string would forge a route — asking a server
  for something the caller never named. The check is a whitelist, not an
  escape, because escaping is where this kind of bug lives.
* **Nothing sensitive is written down.** A failed request is logged with its
  body, and a body can hold a token. Anything whose key looks like a secret
  is replaced outright rather than shortened, long strings are cut, and long
  lists are cut — a log that costs a megabyte per failure is a log nobody
  keeps.

# uv-package: provide-uterm-client

Usage (from the repository root)::

    uv run --package provide-uterm-client python \\
        packages/provide-uterm-ts/testdata/gen_hijackclient_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.client import hijack

OUT = Path(__file__).resolve().parent / "hijackclient_golden.json"

IDS: list[tuple[str, str]] = [
    ("an ordinary id", "worker-1"),
    ("digits", "12345"),
    ("a dotted id", "worker.1"),
    ("an underscored id", "worker_1"),
    ("one character", "a"),
    ("a long id", "w" * 200),
    ("nothing at all", ""),
    ("a dot", "."),
    ("two dots", ".."),
    ("three dots", "..."),
    ("a slash", "a/b"),
    ("a leading slash", "/a"),
    ("a trailing slash", "a/"),
    ("an escaped slash", "a%2Fb"),
    ("a traversal", "../../etc/passwd"),
    ("a query string", "a?x=1"),
    ("a fragment", "a#b"),
    ("a space", "a b"),
    ("a newline", "a\nb"),
    ("a null byte", "a\x00b"),
    ("a colon", "a:b"),
    ("an at sign", "a@b"),
    ("text outside ASCII", "wörker"),
    # An Arabic-Indic digit: `str.isdigit()` says yes, this alphabet says no.
    ("a digit outside ASCII", "worker-١"),  # noqa: RUF001
    ("only spaces", "   "),
]

SANITIZED: list[tuple[str, Any]] = [
    ("nothing", None),
    ("a number", 42),
    ("a short string", "hello"),
    ("a long string", "x" * 600),
    ("a string of exactly the limit", "x" * 500),
    ("a string one past the limit", "x" * 501),
    ("a token", {"token": "s3cret"}),
    ("a token in capitals", {"TOKEN": "s3cret"}),
    ("a name merely containing token", {"my_token_thing": "s3cret"}),
    ("a secret", {"secret": "s3cret"}),
    ("a password", {"password": "hunter2"}),
    ("a key", {"api_key": "s3cret"}),
    ("an authorization header", {"authorization": "Bearer x"}),
    ("a session id", {"session_id": "sess-1"}),
    ("something innocuous", {"worker_id": "w1", "count": 3}),
    ("a mixture", {"worker_id": "w1", "token": "s3cret", "nested": {"password": "x", "n": 1}}),
    ("a short list", [1, 2, 3]),
    ("a list of exactly ten", list(range(10))),
    ("a list of eleven", list(range(11))),
    ("a long list", list(range(50))),
    ("a list of things to redact", [{"token": "a"}, {"token": "b"}]),
    ("a long string inside a list", ["y" * 600]),
    ("a long string inside a mapping", {"note": "y" * 600}),
    ("something deeply nested", {"a": {"b": {"c": {"token": "s3cret", "fine": 1}}}}),
    ("an empty mapping", {}),
    ("an empty list", []),
]


def _guarded(value: str) -> dict[str, Any]:
    try:
        return {"ok": True, "value": hijack._safe_id(value, "worker_id")}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


def main() -> None:
    corpus = {
        "ids": [{"name": name, "id": value, **_guarded(value)} for name, value in IDS],
        "sanitized": [
            {"name": name, "value": value, "sanitized": hijack._sanitize(value)} for name, value in SANITIZED
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['ids'])} ids)")


if __name__ == "__main__":
    main()
