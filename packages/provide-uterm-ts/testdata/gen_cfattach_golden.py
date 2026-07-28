#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-cloudflare
"""Generate the differential golden corpus for WebSocket attachment parsing.

A Durable Object can be evicted and resumed with its sockets still open, and
when that happens the only thing it knows about a connection is the string it
attached at connect time. That string carries who the connection is and what
they are allowed to do, so reading it is an authorisation decision made
without any of the context that produced it.

**The role is the middle field of three.** The attachment is
``type:browser_role:worker_id``, split at most twice so the second field is
the bare role and not the role with the session id stuck to it. A worker id
containing a colon — which nothing forbids — would otherwise make the role
unreadable and silently demote the connection.

**An unreadable role fails closed.** In JWT mode a connection whose role
cannot be recovered is a viewer, not whatever it was before. That is the
post-hibernation case: the instance attribute set at connect time does not
survive eviction, so the attachment is all there is.

**Only the three known roles are accepted.** Anything else in that field is
not a role, and treating it as one would admit whatever an attachment happened
to contain.

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_cfattach_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.cloudflare.do.session_runtime.ws_helpers import _WsHelperMixin

OUT = Path(__file__).with_name("cfattach_golden.json")


class _Jwt:
    """Just the auth mode, which decides the fail-closed default."""

    def __init__(self, mode: str) -> None:
        self.mode = mode


class _Config:
    """Just the configuration the helpers read."""

    def __init__(self, mode: str) -> None:
        self.jwt = _Jwt(mode)


class _Host(_WsHelperMixin):
    """The smallest host the helpers will run against."""

    def __init__(self, mode: str = "jwt", worker_id: str = "w-default") -> None:
        self.config = _Config(mode)
        self.worker_id = worker_id


class _Socket:
    """A connection carrying an attachment."""

    def __init__(self, attachment: Any, *, raises: bool = False) -> None:
        self._attachment = attachment
        self._raises = raises

    def deserializeAttachment(self) -> Any:  # noqa: N802 - the runtime's own name
        if self._raises:
            raise RuntimeError("attachment unreadable")
        return self._attachment


class _Mapping:
    """An attachment that answers like a mapping."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, key: str) -> Any:
        return self._data.get(key)


class _Attributes:
    """An attachment that answers like an object."""

    def __init__(self, role: str) -> None:
        self.role = role


# (name, attachment) — what a connection's type resolves to.
TYPE_CASES: list[tuple[str, Any]] = [
    ("a plain type", "browser"),
    ("a worker", "worker"),
    ("a raw connection", "raw"),
    ("a type with a role after it", "worker:admin"),
    ("a type with a role and a session", "browser:operator:w1"),
    ("an unknown type", "nonsense"),
    ("an unknown type with fields", "nonsense:admin:w1"),
    ("nothing at all", ""),
    ("a mapping", _Mapping({"role": "worker"})),
    ("a mapping with an unknown role", _Mapping({"role": "nonsense"})),
    ("a mapping with no role", _Mapping({})),
    ("an object", _Attributes("raw")),
    ("an object with an unknown role", _Attributes("nonsense")),
    ("a number", 7),
    ("nothing", None),
]

# (name, attachment, mode) — what a browser connection is allowed to do.
ROLE_CASES: list[tuple[str, Any, str]] = [
    ("an admin", "browser:admin:w1", "jwt"),
    ("an operator", "browser:operator:w1", "jwt"),
    ("a viewer", "browser:viewer:w1", "jwt"),
    ("a role with no session after it", "browser:admin", "jwt"),
    # A session id carrying a colon. Split any further and the role would read
    # as "admin:w1" and fail the membership test, silently demoting them.
    ("a session id containing a colon", "browser:admin:w1:extra", "jwt"),
    ("an unknown role", "browser:root:w1", "jwt"),
    ("an empty role", "browser::w1", "jwt"),
    ("no role field at all", "browser", "jwt"),
    ("an attachment that is not a string", _Mapping({"role": "admin"}), "jwt"),
    ("nothing at all", "", "jwt"),
    ("nothing", None, "jwt"),
]

# (name, attachment) — which session a connection belongs to.
WORKER_ID_CASES: list[tuple[str, Any]] = [
    ("a session id", "browser:admin:w1"),
    ("a session id containing a colon", "browser:admin:w1:extra"),
    ("no session field", "browser:admin"),
    ("an empty session field", "browser:admin:"),
    ("an attachment that is not a string", _Mapping({"worker_id": "w9"})),
    ("nothing at all", ""),
]


def _record(cases: list[tuple[str, Any]], read: Any) -> list[dict[str, Any]]:
    """Run one reader over every attachment."""
    out = []
    for name, attachment in cases:
        out.append({"name": name, "attachment": _describe(attachment), "result": read(_Socket(attachment))})
    return out


def _describe(attachment: Any) -> Any:
    """An attachment as JSON can carry it."""
    if isinstance(attachment, _Mapping):
        return {"kind": "mapping", "data": attachment._data}
    if isinstance(attachment, _Attributes):
        return {"kind": "object", "role": attachment.role}
    return {"kind": "value", "value": attachment}


def main() -> int:
    """Write the golden corpus and report what it covers."""
    host = _Host()
    corpus = {
        "types": _record(TYPE_CASES, host._socket_role),
        "roles": [
            {
                "name": name,
                "attachment": _describe(attachment),
                "mode": mode,
                "result": _Host(mode)._socket_browser_role(_Socket(attachment)),
            }
            for name, attachment, mode in ROLE_CASES
        ],
        "worker_ids": _record(WORKER_ID_CASES, host._socket_worker_id),
        "default_worker_id": host.worker_id,
        # A connection whose attachment cannot be read at all.
        "unreadable_type": host._socket_role(_Socket(None, raises=True)),
        "unreadable_role": host._socket_browser_role(_Socket(None, raises=True)),
        "unreadable_worker_id": host._socket_worker_id(_Socket(None, raises=True)),
        # The open-access modes the Worker configuration no longer permits.
        "open_mode_role": _Host("none")._socket_browser_role(_Socket(None, raises=True)),
        "dev_mode_role": _Host("dev")._socket_browser_role(_Socket(None, raises=True)),
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(TYPE_CASES)} types, {len(ROLE_CASES)} roles)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
