#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript worker-link port.

``TermBridge`` is the worker's end of the hub connection. Most of it is loop
plumbing, but four decisions in it are worth recording exactly.

**The URL rewrite.** An operator configures an HTTP manager URL; the bridge
has to reach it over WebSocket. Getting the scheme swap wrong on the secure
side would silently downgrade the connection to plaintext.

**The reconnect schedule.** A fixed ladder that saturates rather than growing
without bound, and resets on a successful connect. Some failures are
permanent — a rejected token or a wrong URL will never resolve — and retrying
those forever is how a fleet of workers turns into a denial of service
against its own hub.

**The resize coercion.** Sizes arrive off the wire and go straight to a PTY
ioctl. ``_safe_int`` is CPython's ``int()`` with a floor and a fallback: it
takes a numeric string, truncates a float toward zero, and refuses anything
else — including a string that merely looks like a float.

**The frame dispatch.** Which control action maps to which worker call.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_worker_link_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.server.bridge.models import _safe_int
from provide.uterm.server.bridge.worker_link import TermBridge, _encode_bridge_frame, _to_ws_url

OUT = Path(__file__).with_name("worker_link_golden.json")

# (name, manager url, path)
URL_CASES: list[tuple[str, str, str]] = [
    ("https", "https://hub.example", "/ws/worker/w1/term"),
    ("http", "http://hub.example", "/ws/worker/w1/term"),
    ("https with a trailing slash", "https://hub.example/", "/ws/worker/w1/term"),
    ("http with several trailing slashes", "http://hub.example///", "/ws/worker/w1/term"),
    ("https with a port", "https://hub.example:8443", "/ws/worker/w1/term"),
    ("https with a base path", "https://hub.example/uterm", "/ws/worker/w1/term"),
    ("already wss", "wss://hub.example", "/ws/worker/w1/term"),
    ("already ws", "ws://hub.example", "/ws/worker/w1/term"),
    ("no scheme", "hub.example", "/ws/worker/w1/term"),
    ("empty", "", "/ws/worker/w1/term"),
    ("uppercase scheme is not rewritten", "HTTPS://hub.example", "/ws/worker/w1/term"),
    ("http inside the host is not rewritten", "https://http.example", "/ws/worker/w1/term"),
    # A scheme appearing later in the URL must not be treated as the scheme.
    # Matching anywhere rather than at the start would slice the wrong prefix
    # and produce an unreachable address.
    ("scheme-like text after another scheme", "ftp://hub.example/http://inner", "/ws/worker/w1/term"),
    ("scheme-like text in a path", "https://hub.example/proxy/http://inner", "/ws/worker/w1/term"),
]

# (name, message)
FRAME_CASES: list[tuple[str, dict[str, Any]]] = [
    ("term data", {"type": "term", "data": "hello"}),
    ("term with no data", {"type": "term"}),
    ("status", {"type": "status", "hijacked": True, "ts": 1.5}),
    ("snapshot", {"type": "snapshot", "screen": "x"}),
    ("no type", {"data": "hello"}),
    ("null type", {"type": None, "data": "hello"}),
]

# (name, value, default, min_val)
SAFE_INT_CASES: list[tuple[str, Any, int, int | None]] = [
    ("integer", 40, 80, 1),
    ("zero with a floor", 0, 25, 1),
    ("zero with no floor", 0, 25, None),
    ("negative with a floor", -1, 80, 1),
    ("negative with no floor", -1, 80, None),
    ("numeric string", "123", 0, None),
    ("numeric string with spaces", " 123 ", 0, None),
    ("float", 40.9, 80, 1),
    ("negative float", -40.9, 80, None),
    ("float string is refused", "1.5", 80, None),
    ("empty string", "", 80, None),
    ("word", "bad", 80, None),
    ("none", None, 42, None),
    ("list", [1, 2], 25, None),
    ("bool true", True, 80, None),
    ("bool false", False, 80, None),
    ("at the floor", 1, 80, 1),
    ("underscore separated", "1_0", 80, None),
    ("unicode digits", "٣", 80, None),
    # int() takes Unicode *decimal* digits but not the wider isdigit set, so a
    # superscript is refused where an Arabic-Indic digit is accepted.
    ("superscript digit is refused", "²", 80, None),
    ("leading plus", "+7", 80, None),
    ("leading minus", "-7", 80, None),
    ("leading underscore is refused", "_10", 80, None),
    ("trailing underscore is refused", "10_", 80, None),
    ("doubled underscore is refused", "1__0", 80, None),
    ("newline padding", "\n12\t", 80, None),
    ("hex is refused", "0x10", 80, None),
    ("plus with spaces is refused", "+ 7", 80, None),
    ("very large", "9" * 30, 80, None),
    # The reference coerces the *default* only on the None path — int(default)
    # there, but a bare return of default everywhere else. A fractional
    # default therefore comes back truncated for None and intact otherwise.
    ("none with a fractional default", None, 25.7, None),
    ("word with a fractional default", "bad", 25.7, None),
]


def main() -> int:
    """Write the golden corpus and report the case count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_worker_link_golden.py",
        "reconnect_backoff": list(TermBridge._RECONNECT_BACKOFF),
        "permanent_statuses": [401, 403, 404],
        "urls": [
            {"name": name, "manager_url": manager_url, "path": path, "url": _to_ws_url(manager_url, path)}
            for name, manager_url, path in URL_CASES
        ],
        "frames": [
            {"name": name, "message": message, "encoded": _encode_bridge_frame(message)}
            for name, message in FRAME_CASES
        ],
        "safe_ints": [
            {
                "name": name,
                "value": value,
                "default": default,
                "min_val": min_val,
                "result": _safe_int(value, default, min_val=min_val),
            }
            for name, value, default, min_val in SAFE_INT_CASES
        ],
        "resize_defaults": {"cols": 80, "rows": 25, "min": 1},
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(payload['urls'])} url cases, {len(payload['safe_ints'])} coercion cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
