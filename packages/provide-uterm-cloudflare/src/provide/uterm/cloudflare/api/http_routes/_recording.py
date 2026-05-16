#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Recording route handlers for the Cloudflare Durable Object session runtime.

Exposes existing ``session_events`` data through recording-compatible routes
so the replay frontend can consume it without changes.

Routes
------
``GET /api/sessions/{id}/recording``           — metadata (enabled, entry count)
``GET /api/sessions/{id}/recording/entries``   — paginated entries as ``{ts, event, data}``
``GET /api/sessions/{id}/recording/download``  — full JSONL stream
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from ._shared import _safe_int

try:
    from provide.uterm.cloudflare.cf_types import json_response
except ImportError:  # pragma: no cover
    from cf_types import json_response  # type: ignore[import-not-found]  # CF flat path

if TYPE_CHECKING:
    import re

    from provide.uterm.cloudflare.contracts import RuntimeProtocol


async def route_recording(
    runtime: RuntimeProtocol,
    _request: object,
    url: str,
    match: re.Match[str],
) -> object:
    """Dispatch recording sub-routes.

    *match* groups: (1) session_id, (2) optional sub-path (``"entries"`` or None).
    """
    session_id = match.group(1)
    sub = match.group(2)  # None or "entries"

    if session_id != runtime.worker_id:
        return json_response({"error": "not_found"}, status=404)

    if sub is None:
        return _recording_meta(runtime, session_id)

    if sub == "entries":
        return _recording_entries(runtime, session_id, url)

    if sub == "download":
        return _recording_download(runtime, session_id)

    return json_response({"error": "not_found"}, status=404)


def _recording_meta(runtime: RuntimeProtocol, session_id: str) -> object:
    count = runtime.store.count_events(runtime.worker_id)
    return json_response(
        {
            "session_id": session_id,
            "enabled": True,
            "entry_count": count,
            "exists": count > 0,
        }
    )


def _recording_entries(runtime: RuntimeProtocol, _session_id: str, url: str) -> object:
    qs = parse_qs(urlparse(url).query)
    limit = _safe_int(qs.get("limit", [None])[0], 200, min_val=1, max_val=500)
    raw_offset = qs.get("offset", [None])[0]
    offset: int | None = None if raw_offset is None else _safe_int(raw_offset, 0, min_val=0)
    event = qs.get("event", [None])[0]

    entries = runtime.store.list_recording_entries(
        runtime.worker_id,
        limit=limit,
        offset=offset,
        event=event,
    )
    return json_response(entries)


def _recording_download(runtime: RuntimeProtocol, session_id: str) -> object:
    """Stream all events as JSONL (one JSON object per line)."""
    try:
        from provide.uterm.cloudflare.cf_types import Response
    except ImportError:  # pragma: no cover
        from cf_types import Response  # type: ignore[import-not-found]

    entries = runtime.store.list_recording_entries(runtime.worker_id, limit=500, offset=0)
    lines = [json.dumps(e, ensure_ascii=True) for e in entries]
    body = "\n".join(lines) + ("\n" if lines else "")
    return Response(
        body,
        status=200,
        headers={
            "content-type": "application/x-ndjson",
            "content-disposition": f'attachment; filename="{session_id}.jsonl"',
        },
    )
