#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the Worker's route matching.

The three route matchers a request meets before anything asks who it is, each
driven rather than described.

* **The public checks.** The health path, the ``/assets/`` prefix, and a
  static-file pattern that is a closed set rather than a blocklist: only
  ``.html``, ``.css`` and ``.js``, and only names made of letters, digits,
  dots, slashes, dashes and underscores. Anything outside it is *not* served
  here — it falls through to the routes that do ask. Widening any of these
  three is how a Worker starts handing out pages to nobody in particular.
* **The single-page routes.** Which page kind a path names, and which surface
  it gets: an inspect, replay or operator page is an operator surface, and a
  session page is a user one.
* **The Durable Object routes.** Six patterns, each bounding the session id to
  64 characters of a closed alphabet — so a proxied path cannot name an object
  by a string that a filesystem or a KV key would read differently.

What is deliberately *not* recorded here is the order those matchers run in
relative to the API dispatch table, which is a separate unit: inventing a
classification that depended on it would record a reading rather than a run.

# uv-package: provide-uterm-cloudflare

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_workerroutes_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.cloudflare.entry import handlers, registry, spa

OUT = Path(__file__).resolve().parent / "workerroutes_golden.json"

PATHS: list[tuple[str, str]] = [
    ("the health check", "/api/health"),
    ("an asset", "/assets/app.js"),
    ("an asset in a folder", "/assets/vendor/xterm.css"),
    ("an asset path and nothing else", "/assets/"),
    ("a static page", "/index.html"),
    ("a static stylesheet", "/theme.css"),
    ("a static script", "/bundle.js"),
    ("a static file in a folder", "/static/app.js"),
    ("a static file with a dash and an underscore", "/my-app_v2.js"),
    ("a file of a kind that is not served", "/config.json"),
    ("a file with no extension", "/README"),
    ("a path climbing out with dots", "/../etc/passwd"),
    ("a path with an encoded slash", "/a%2Fb.js"),
    ("a path with a space in it", "/my app.js"),
    ("a path with a query on it", "/app.js?v=1"),
    ("the root", "/"),
    ("the app root", "/app"),
    ("the app root with a slash", "/app/"),
    ("the connect page", "/app/connect"),
    ("the connect page with a slash", "/app/connect/"),
    ("a share link", "/s/sess-1"),
    ("a share link with a slash", "/s/sess-1/"),
    ("a session page", "/app/session/sess-1"),
    ("an inspect page", "/app/inspect/sess-1"),
    ("a replay page", "/app/replay/sess-1"),
    ("an operator page", "/app/operator/sess-1"),
    ("a page kind nobody defined", "/app/hijack/sess-1"),
    ("a browser socket", "/ws/browser/sess-1"),
    ("a worker socket", "/ws/worker/sess-1"),
    ("a tunnel", "/tunnel/sess-1"),
    ("a session API route", "/api/sessions/sess-1"),
    ("a path nobody routes", "/nowhere"),
]


def _public(path: str) -> dict[str, Any]:
    """The three checks ``_route_request`` makes before it demands anything.

    Only these are recorded: the dispatch table that follows them is a separate
    unit, and inventing a classification that depended on it would record a
    reading rather than a run.
    """
    return {
        "health": path == "/api/health",
        "asset": path.startswith("/assets/"),
        "asset_name": path.removeprefix("/assets/") if path.startswith("/assets/") else None,
        "static": handlers._STATIC_ASSET_PATH.match(path) is not None,
        "static_name": path.removeprefix("/") if handlers._STATIC_ASSET_PATH.match(path) else None,
    }


def main() -> None:
    corpus = {
        "static_asset_pattern": handlers._STATIC_ASSET_PATH.pattern,
        "do_route_patterns": [pattern.pattern for pattern in registry._NATIVE_DO_ROUTE_PATTERNS],
        "paths": [{"name": name, "path": path, **_public(path)} for name, path in PATHS],
        "spa": [
            {"path": path, "resolved": _spa(path)}
            for path in (
                "/",
                "/app",
                "/app/",
                "/app/connect",
                "/app/connect/",
                "/app/connect/extra",
                "/appconnect",
                "/s/sess-1",
                "/s/sess-1/",
                "/s/",
                "/s",
                "/app/session/sess-1",
                "/app/inspect/sess-1",
                "/app/replay/sess-1",
                "/app/operator/sess-1",
                "/app/hijack/sess-1",
                "/app/session/",
                "/app/session",
                "/app/session/a/b",
                "/nowhere",
            )
        ],
        "worker_ids": [
            {"path": path, "worker_id": registry._extract_worker_id(path)}
            for path in (
                "/ws/browser/sess-1/term",
                "/ws/worker/sess-1/term",
                "/ws/raw/sess-1/term",
                "/ws/browser/sess-1",
                "/tunnel/sess-1",
                "/tunnel/sess-1/extra",
                "/worker/sess-1/hijack",
                "/worker/sess-1/hijack/release",
                "/worker/sess-1/input_mode",
                "/worker/sess-1/disconnect_worker",
                "/worker/sess-1/anything_else",
                "/worker/sess-1",
                "/tunnel/" + "a" * 64,
                "/tunnel/" + "a" * 65,
                "/tunnel/a.b",
                "/tunnel/a%2Fb",
                "/tunnel/",
                "/api/sessions/sess-1",
                "/nowhere",
                "/",
            )
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['paths'])} paths)")


def _spa(path: str) -> dict[str, Any] | None:
    resolved = spa._resolve_spa_route(path)
    if resolved is None:
        return None
    kind, extra = resolved
    return {"kind": kind, "extra": dict(extra)}


if __name__ == "__main__":
    main()
