# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tunnel intercept stress script for memray profiling.

Exercises the per-request InterceptGate cycle (future create -> resolve) and
the ``parse_action_message`` decode path (base64 body + header sanitization)
that runs on every intercepted HTTP request/response.

Workload: 5_000 intercepted requests, each cycled through await_decision +
resolve with a ``modify`` action that carries headers + a base64 body.
Run via: python -m memray run -o intercept_stress.bin scripts/memray_intercept_stress.py
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from provide.uterm.tunnel.intercept import InterceptGate, parse_action_message

# The intercept module logs once per denylisted-header batch. Without this the
# allocation count is dominated by logging noise rather than the dispatch path.
logging.getLogger("provide.uterm.tunnel.intercept").setLevel(logging.ERROR)

NUM_REQUESTS = 5_000

# Realistic body — one chunk of HTML-ish content per request. Pre-encoded
# once and reused so we measure decode + InterceptGate, not encode noise.
_BODY = (b"<html><body>" + b"x" * 256 + b"</body></html>") * 2
_BODY_B64 = base64.b64encode(_BODY).decode()

# Mixed sanitizable + safe headers so ``_sanitize_headers`` exercises both
# branches (drop + keep) on every iteration.
_HEADERS: dict[str, str] = {
    "User-Agent": "uterm-stress/1.0",
    "Accept": "text/html,application/xhtml+xml",
    "Content-Type": "text/html",
    "X-Trace-Id": "abc123",
    # Denylisted — must be dropped:
    "Authorization": "Bearer should-be-stripped",
    "Cookie": "session=should-be-stripped",
    "Content-Length": "999",
}


def _make_action(rid: str) -> dict[str, Any]:
    return {
        "id": rid,
        "action": "modify",
        "headers": _HEADERS,
        "body_b64": _BODY_B64,
    }


async def _resolver(gate: InterceptGate, rid: str, decision: dict[str, Any]) -> None:
    """Schedule a parse + resolve for the pending request *rid*."""
    parsed = parse_action_message(decision)
    gate.resolve(rid, parsed)


async def main() -> None:
    gate = InterceptGate(timeout_s=5.0)
    gate.enabled = True

    for i in range(NUM_REQUESTS):
        rid = f"req-{i:06d}"
        action = _make_action(rid)
        # Schedule the browser-side resolve concurrently with await_decision so
        # we measure the full intercept cycle, not just one side.
        resolver_task = asyncio.create_task(_resolver(gate, rid, action))
        decision = await gate.await_decision(rid)
        await resolver_task
        # Touch the decision so the compiler can't optimize it away.
        assert decision["action"] == "modify"
        assert decision["body"] == _BODY


if __name__ == "__main__":
    asyncio.run(main())
