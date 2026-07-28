#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-cloudflare
"""Generate the differential golden corpus for the session's socket registry.

Which connections a Durable Object is holding, and everything that has to be
let go when one ends.

**A worker has one slot; browsers and raw connections have many.** A session
has exactly one worker at a time, so registering a second replaces the first
rather than accumulating.

**Removing a connection clears everything keyed by it**, not merely the
registry: the hijack it owned, its resume token and its flow-control
accounting all go too. Anything left behind would be state for a connection
that no longer exists, and the flow controller in particular would keep
counting a browser that can never acknowledge again.

**Only the socket that *is* the worker clears the worker slot.** Comparing by
identity rather than by id, because a browser disconnecting must not detach
the worker from the session.

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_socketregistry_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.cloudflare.do.session_runtime.ws_helpers import _WsHelperMixin

OUT = Path(__file__).with_name("socketregistry_golden.json")


class _Socket:
    """A connection carrying an attachment."""

    def __init__(self, attachment: Any) -> None:
        self._attachment = attachment

    def deserializeAttachment(self) -> Any:  # noqa: N802 - the runtime's own name
        return self._attachment


class _Flow:
    """A flow controller that records what it was told to forget."""

    def __init__(self) -> None:
        self.forgotten: list[str] = []

    def forget(self, ws_id: str) -> None:
        self.forgotten.append(ws_id)


class _Host(_WsHelperMixin):
    """The smallest host the registry helpers run against."""

    def __init__(self) -> None:
        self.worker_ws: Any = None
        self.browser_sockets: dict[str, Any] = {}
        self.raw_sockets: dict[str, Any] = {}
        self.browser_hijack_owner: dict[str, str] = {}
        self.browser_resume_tokens: dict[str, str] = {}
        self._flow = _Flow()


def _snapshot(host: _Host, keys: dict[str, str]) -> dict[str, Any]:
    """What the registries hold, named by the sockets that were registered."""
    reverse = {value: name for name, value in keys.items()}
    return {
        "worker": host.worker_ws is not None,
        "browsers": sorted(reverse.get(key, key) for key in host.browser_sockets),
        "raw": sorted(reverse.get(key, key) for key in host.raw_sockets),
        "hijack_owners": sorted(reverse.get(key, key) for key in host.browser_hijack_owner),
        "resume_tokens": sorted(reverse.get(key, key) for key in host.browser_resume_tokens),
        "forgotten": [reverse.get(key, key) for key in host._flow.forgotten],
    }


def _build() -> dict[str, Any]:
    """A session filling up and emptying again."""
    host = _Host()
    sockets = {
        "worker": _Socket("worker"),
        "browser-a": _Socket("browser:admin:w1"),
        "browser-b": _Socket("browser:viewer:w1"),
        "raw-a": _Socket("raw"),
    }
    keys: dict[str, str] = {}
    steps: list[dict[str, Any]] = []

    def record(name: str) -> None:
        steps.append({"name": name, **_snapshot(host, keys)})

    for name, socket in sockets.items():
        role = "worker" if name == "worker" else "raw" if name.startswith("raw") else "browser"
        host._register_socket(socket, role)
        keys[name] = host.ws_key(socket)
        record(f"registered {name}")

    # State keyed by a browser, which removal has to clear as well.
    host.browser_hijack_owner[keys["browser-a"]] = "u1"
    host.browser_resume_tokens[keys["browser-a"]] = "t1"
    record("browser-a owns the hijack")

    host._remove_ws(sockets["browser-a"])
    record("browser-a leaves")
    host._remove_ws(sockets["raw-a"])
    record("raw-a leaves")
    host._remove_ws(sockets["worker"])
    record("the worker leaves")
    host._remove_ws(sockets["browser-b"])
    record("browser-b leaves")

    # A second worker replaces the first rather than accumulating.
    replaced = _Host()
    first, second = _Socket("worker"), _Socket("worker")
    replaced._register_socket(first, "worker")
    replaced._register_socket(second, "worker")

    # Removing a browser must not detach the worker.
    detach = _Host()
    worker, browser = _Socket("worker"), _Socket("browser:admin:w1")
    detach._register_socket(worker, "worker")
    detach._register_socket(browser, "browser")
    detach._remove_ws(browser)

    return {
        "steps": steps,
        "worker_replaced": replaced.worker_ws is second,
        "worker_survives_browser_removal": detach.worker_ws is worker,
        # A key is stable for one socket and different for another.
        "key_is_stable": host.ws_key(sockets["browser-b"]) == host.ws_key(sockets["browser-b"]),
        "keys_differ": host.ws_key(sockets["browser-b"]) != host.ws_key(sockets["raw-a"]),
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['steps'])} steps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
