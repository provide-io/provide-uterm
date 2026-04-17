#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from ._shared import (
    _extract_prompt_id,
    _safe_int,
    _session_status_item,
    _wait_for_analysis,
)

try:
    from provide.terminal.cloudflare.cf_types import json_response
except ImportError:  # pragma: no cover
    from cf_types import json_response  # type: ignore[import-not-found]  # CF flat path  # pragma: no cover

if TYPE_CHECKING:
    import re

    from provide.terminal.cloudflare.contracts import RuntimeProtocol


async def route_session(
    runtime: RuntimeProtocol,
    request: object,
    path: str,
    url: str,
    method: str,
    session_match: re.Match[str],
) -> object:
    """Handle /api/sessions/{id}[/sub] routes."""
    session_id, sub = session_match.group(1), (session_match.group(2) or "")
    if session_id != runtime.worker_id:
        return json_response({"error": "not_found", "path": path}, status=404)

    if sub == "" and method == "GET":
        # Visibility is enforced at the dispatch boundary (_dispatch.py route_http)
        # before this handler is reached — no additional check needed here.
        return json_response(_session_status_item(runtime))

    if sub == "snapshot" and method == "GET":
        snapshot2: dict[str, object] | None = runtime.last_snapshot
        if snapshot2 is None:
            row = runtime.store.load_session(runtime.worker_id)
            snapshot2 = row.get("last_snapshot") if row else None
        return json_response(
            {
                "session_id": runtime.worker_id,
                "snapshot": snapshot2,
                "prompt_detected": snapshot2.get("prompt_detected") if snapshot2 else None,
                "prompt_id": _extract_prompt_id(snapshot2),
            }
        )

    if sub == "events" and method == "GET":
        qs = parse_qs(urlparse(url).query)
        after_seq = _safe_int(qs.get("after_seq", ["0"])[0], 0)
        limit = _safe_int(qs.get("limit", ["100"])[0], 100, min_val=1, max_val=500)
        rows = runtime.store.list_events_since(runtime.worker_id, after_seq, limit)
        latest_seq = runtime.store.current_event_seq(runtime.worker_id)
        min_event_seq = runtime.store.min_event_seq(runtime.worker_id)
        return json_response(
            {
                "session_id": runtime.worker_id,
                "after_seq": after_seq,
                "latest_seq": latest_seq,
                "min_event_seq": min_event_seq,
                "has_more": len(rows) >= limit,
                "events": rows,
            }
        )

    if sub == "mode" and method == "POST":
        if await runtime.browser_role_for_request(request) != "admin":
            return json_response({"error": "admin role required"}, status=403)
        payload = await runtime.request_json(request)
        mode = str(payload.get("input_mode") or "")
        if mode not in {"hijack", "open"}:
            return json_response({"error": "input_mode must be 'hijack' or 'open'"}, status=400)
        if mode == "open" and runtime.hijack.session is not None:
            return json_response({"error": "Cannot switch to open while hijack is active."}, status=409)
        runtime.input_mode = mode
        runtime.store.save_input_mode(runtime.worker_id, mode)
        return json_response({"ok": True, "input_mode": mode, "worker_id": runtime.worker_id})

    if sub == "clear" and method == "POST":
        role = await runtime.browser_role_for_request(request)
        if role not in {"operator", "admin"}:
            return json_response({"error": "operator or admin role required"}, status=403)
        runtime.last_snapshot = None
        if runtime.worker_ws is not None:
            await runtime.send_ws(runtime.worker_ws, {"type": "snapshot_req", "ts": time.monotonic()})
        return json_response(_session_status_item(runtime))

    if sub == "analyze" and method == "POST":
        role = await runtime.browser_role_for_request(request)
        if role not in {"operator", "admin"}:
            return json_response({"error": "operator or admin role required"}, status=403)
        ok = await runtime.push_worker_control("analyze", owner="", lease_s=0)
        if not ok:
            return json_response({"error": "no_worker"}, status=409)
        analysis = await _wait_for_analysis(runtime, timeout_ms=5_000)
        return json_response({"ok": True, "analysis": analysis, "worker_id": runtime.worker_id})

    if sub == "" and method == "DELETE":
        role = await runtime.browser_role_for_request(request)
        if role not in {"operator", "admin"}:
            return json_response({"error": "operator or admin role required"}, status=403)
        # Close any active worker connection.
        worker_ws = runtime.worker_ws
        if worker_ws is not None:
            with contextlib.suppress(Exception):
                worker_ws.close(1001, "session deleted")
        return json_response({"ok": True, "session_id": runtime.worker_id, "deleted": True})

    if sub == "restart" and method == "POST":
        role = await runtime.browser_role_for_request(request)
        if role not in {"operator", "admin"}:
            return json_response({"error": "operator or admin role required"}, status=403)
        # Clear in-memory terminal state so a fresh worker starts clean.
        runtime.last_snapshot = None
        # If a worker is connected, close its socket — the Python bridge will
        # reconnect and restart the underlying connector automatically.
        worker_ws = runtime.worker_ws
        if worker_ws is not None:
            with contextlib.suppress(Exception):
                worker_ws.close(1001, "restart requested")
        return json_response({**_session_status_item(runtime), "restarted": True})

    return json_response({"error": "not_found", "path": path}, status=404)
