#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for HTTP request interception.

Interception hands an operator's browser the ability to rewrite a request
before it is forwarded. That is the whole feature and also the whole danger,
so what the rewrite is *not* allowed to touch is the part worth pinning:

* **Hop-by-hop headers** are connection-scoped and must not be proxied at all.
* **Framing headers** — a `Content-Length` the operator chose — are how
  request smuggling is done against whatever is downstream.
* **Identity headers** — `Host`, `Authorization`, `Cookie`, the forwarding
  family — would let the operator impersonate the original requester or take
  over authentication downstream, which is the very thing the interceptor
  exists to make visible.

Matched without regard to case, because a header named `AUTHORIZATION` is the
same header.

The rest is the gate: a request waits for a decision, and if none arrives it
is released the configured way rather than hanging. A decision naming an
action nobody defined becomes `forward` — the choice that keeps traffic
moving rather than the one that silently drops it.

Everything is driven against the real gate and the real parser.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_intercept_golden.py
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from provide.uterm.tunnel import intercept

OUT = Path(__file__).resolve().parent / "intercept_golden.json"


def _decision(value: Any) -> dict[str, Any]:
    """A decision, with its body rendered so JSON can hold it."""
    body = value["body"]
    return {
        "action": value["action"],
        "headers": value["headers"],
        "body": None if body is None else base64.b64encode(body).decode("ascii"),
    }


MESSAGES: list[tuple[str, dict[str, Any]]] = [
    ("forwarding", {"action": "forward"}),
    ("dropping", {"action": "drop"}),
    ("modifying nothing", {"action": "modify"}),
    ("no action at all", {}),
    ("an action nobody defined", {"action": "sideways"}),
    ("an action in capitals", {"action": "FORWARD"}),
    ("an action that is not text", {"action": 42}),
    ("an action that is null", {"action": None}),
    (
        "modifying an ordinary header",
        {"action": "modify", "headers": {"X-Trace": "1", "Accept": "text/plain"}},
    ),
    ("modifying the host", {"action": "modify", "headers": {"Host": "evil.example"}}),
    ("modifying the host in capitals", {"action": "modify", "headers": {"HOST": "evil.example"}}),
    ("modifying authorization", {"action": "modify", "headers": {"Authorization": "Bearer stolen"}}),
    ("modifying a cookie", {"action": "modify", "headers": {"Cookie": "session=stolen"}}),
    ("modifying the length", {"action": "modify", "headers": {"Content-Length": "0"}}),
    ("modifying the transfer encoding", {"action": "modify", "headers": {"Transfer-Encoding": "chunked"}}),
    ("modifying the connection", {"action": "modify", "headers": {"Connection": "close"}}),
    (
        "modifying the forwarding family",
        {
            "action": "modify",
            "headers": {
                "Forwarded": "for=1.2.3.4",
                "X-Forwarded-For": "1.2.3.4",
                "X-Forwarded-Host": "evil.example",
                "X-Forwarded-Proto": "http",
                "X-Real-IP": "1.2.3.4",
            },
        },
    ),
    (
        "modifying one allowed and one denied header",
        {"action": "modify", "headers": {"X-Trace": "1", "Authorization": "Bearer stolen"}},
    ),
    ("headers that are not a mapping", {"action": "modify", "headers": ["Host: evil"]}),
    ("headers whose values are not text", {"action": "modify", "headers": {"X-Count": 5}}),
    ("a body", {"action": "modify", "body_b64": base64.b64encode(b"hello").decode()}),
    ("a body that is not base64", {"action": "modify", "body_b64": "not base64!!"}),
    ("a body with the padding missing", {"action": "modify", "body_b64": "aGVsbG8"}),
    ("a body that is not text", {"action": "modify", "body_b64": 42}),
    ("an empty body", {"action": "modify", "body_b64": ""}),
    ("a body on a forward", {"action": "forward", "body_b64": base64.b64encode(b"hello").decode()}),
    ("headers on a drop", {"action": "drop", "headers": {"X-Trace": "1"}}),
]


async def _gate_cases() -> list[dict[str, Any]]:
    """The gate itself: what it starts as, and what happens to a waiting request."""
    cases: list[dict[str, Any]] = []

    for name, timeout_s, timeout_action in (
        ("the defaults", 30.0, "forward"),
        ("a short timeout", 2.0, "drop"),
        ("a timeout below the floor", 0.1, "forward"),
        ("a negative timeout", -5.0, "forward"),
        ("a timeout action nobody defined", 5.0, "sideways"),
        ("a timeout action of drop", 5.0, "drop"),
    ):
        gate = intercept.InterceptGate(timeout_s=timeout_s, timeout_action=timeout_action)
        cases.append(
            {
                "name": name,
                "given": {"timeout_s": timeout_s, "timeout_action": timeout_action},
                "enabled": gate.enabled,
                "inspect_enabled": gate.inspect_enabled,
                "timeout_s": gate.timeout_s,
                "timeout_action": gate.timeout_action,
                "pending_count": gate.pending_count,
            }
        )
    return cases


async def _flow_cases() -> list[dict[str, Any]]:
    """What a waiting request ends up with, driven for real."""
    cases: list[dict[str, Any]] = []

    # A decision arriving while the request waits.
    gate = intercept.InterceptGate(timeout_s=5.0)
    waiting = asyncio.ensure_future(gate.await_decision("r1"))
    await asyncio.sleep(0)
    pending_while_waiting = gate.pending_count
    resolved = gate.resolve("r1", intercept.parse_action_message({"action": "drop"}))
    cases.append(
        {
            "name": "a decision arriving",
            "pending_while_waiting": pending_while_waiting,
            "resolved": resolved,
            "decision": _decision(await waiting),
            "pending_after": gate.pending_count,
        }
    )

    # A decision for a request nobody is waiting on.
    gate = intercept.InterceptGate(timeout_s=5.0)
    cases.append(
        {
            "name": "a decision nobody was waiting for",
            "pending_while_waiting": 0,
            "resolved": gate.resolve("r-unknown", intercept.parse_action_message({"action": "drop"})),
            "decision": None,
            "pending_after": gate.pending_count,
        }
    )

    # Two decisions for the same request.
    gate = intercept.InterceptGate(timeout_s=5.0)
    waiting = asyncio.ensure_future(gate.await_decision("r1"))
    await asyncio.sleep(0)
    first = gate.resolve("r1", intercept.parse_action_message({"action": "drop"}))
    second = gate.resolve("r1", intercept.parse_action_message({"action": "forward"}))
    cases.append(
        {
            "name": "a second decision for the same request",
            "pending_while_waiting": 1,
            "resolved": first and not second,
            "decision": _decision(await waiting),
            "pending_after": gate.pending_count,
        }
    )

    # Nothing arrives at all.
    gate = intercept.InterceptGate(timeout_s=1.0, timeout_action="drop")
    cases.append(
        {
            "name": "nothing arriving before the timeout",
            "pending_while_waiting": 1,
            "resolved": False,
            "decision": _decision(await gate.await_decision("r1")),
            "pending_after": gate.pending_count,
        }
    )

    # Everything released at once.
    gate = intercept.InterceptGate(timeout_s=5.0)
    waits = [asyncio.ensure_future(gate.await_decision(f"r{index}")) for index in range(3)]
    await asyncio.sleep(0)
    released = gate.cancel_all("drop")
    cases.append(
        {
            "name": "everything released at once",
            "pending_while_waiting": 3,
            "resolved": released == 3,
            "decision": _decision(await waits[0]),
            "pending_after": gate.pending_count,
        }
    )

    # Releasing when there is nothing to release.
    gate = intercept.InterceptGate(timeout_s=5.0)
    cases.append(
        {
            "name": "releasing nothing",
            "pending_while_waiting": 0,
            "resolved": gate.cancel_all("forward") == 0,
            "decision": None,
            "pending_after": gate.pending_count,
        }
    )
    return cases


async def main_async() -> None:
    corpus = {
        "denylist": sorted(intercept._DENYLISTED_HEADERS),
        "parsed": [
            {"name": name, "message": message, "decision": _decision(intercept.parse_action_message(message))}
            for name, message in MESSAGES
        ],
        "gates": await _gate_cases(),
        "flows": await _flow_cases(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['parsed'])} messages)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
