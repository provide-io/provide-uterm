#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Polling-SSE endpoint for the CF Durable Object session runtime.

CF DOs cannot hold long-lived HTTP connections across hibernation, so SSE is
implemented as a **polling** pattern:

1. Client sends ``GET /api/sessions/{id}/events/stream?after_seq=N``
   (first request: ``after_seq=0``; on reconnect: value from ``Last-Event-ID``).
2. Server returns up to 100 events since *after_seq* in SSE format with
   ``id: {seq}\\n`` so the client can track its position.
3. A ``retry: 3000\\n\\n`` directive tells EventSource to reconnect in 3 s.
4. The stream closes immediately after the batch (no long-lived connection).

Event format (one per event)::

    id: 42
    data: {"type":"snapshot","seq":42,...}

    id: 43
    data: {"type":"input_send","seq":43,...}

    retry: 3000

"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

try:
    from provide.terminal.cloudflare.cf_types import Response
except ImportError:  # pragma: no cover
    from cf_types import Response  # type: ignore[import-not-found]  # CF flat path  # pragma: no cover

if TYPE_CHECKING:
    from provide.terminal.cloudflare.contracts import RuntimeProtocol

_RETRY_MS = 3000
_MAX_EVENTS = 100


def build_sse_response(events: list[dict], *, retry_ms: int = _RETRY_MS) -> Response:
    """Build a polling-SSE ``Response`` from a list of event dicts.

    Each event should have a ``seq`` field used as the SSE ``id``.
    """
    lines: list[str] = []
    for event in events:
        seq = event.get("seq", "")
        lines.append(f"id: {seq}")
        lines.append(f"data: {json.dumps(event)}")
        lines.append("")  # blank line terminates each event
    lines.append(f"retry: {retry_ms}")
    lines.append("")
    body = "\n".join(lines)
    return Response(
        body,
        status=200,
        headers={
            "content-type": "text/event-stream; charset=utf-8",
            "cache-control": "no-cache",
            "x-accel-buffering": "no",
        },
    )


async def route_sse(
    runtime: RuntimeProtocol,
    request: object,
    url: str,
    session_id: str,
) -> Response:
    """Handle ``GET /api/sessions/{id}/events/stream``."""
    if session_id != runtime.worker_id:
        return Response(
            json.dumps({"error": "not_found"}),
            status=404,
            headers={"content-type": "application/json"},
        )

    qs = parse_qs(urlparse(url).query)
    # Support both query param and Last-Event-ID header for after_seq.
    last_event_id = str(getattr(request, "headers", {}).get("last-event-id", "0") or "0")
    after_seq_raw = qs.get("after_seq", [last_event_id])[0]
    try:
        after_seq = max(0, int(after_seq_raw))
    except (ValueError, TypeError):
        after_seq = 0

    events = runtime.store.list_events_since(runtime.worker_id, after_seq, _MAX_EVENTS)
    return build_sse_response(events)
