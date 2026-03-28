#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from ._shared import (
    _MAX_INPUT_CHARS,
    _MAX_REGEX_LEN,
    _MAX_TIMEOUT_MS,
    PromptRegexError,
    _extract_hijack_id,
    _extract_prompt_id,
    _parse_lease_s,
    _safe_int,
    _wait_for_prompt,
    build_hijack_events_response,
    build_hijack_snapshot_response,
    compile_expect_regex,
)

try:
    from provide.terminal.cloudflare.cf_types import json_response
except ImportError:  # pragma: no cover
    from cf_types import json_response  # type: ignore[import-not-found]  # CF flat path  # pragma: no cover

if TYPE_CHECKING:
    import re

    from provide.terminal.cloudflare.contracts import RuntimeProtocol


async def route_hijack(
    runtime: RuntimeProtocol,
    request: object,
    path: str,
    url: str,
    method: str,
) -> object | None:
    """Handle all /hijack/ routes. Returns a response if matched, or None."""

    if path.endswith("/hijack/acquire") and method == "POST":
        # Hijack requires admin role.
        if await runtime.browser_role_for_request(request) != "admin":
            return json_response({"error": "admin role required"}, status=403)
        payload = await runtime.request_json(request)
        owner = str(payload.get("owner") or "operator")
        lease_s, lease_error = _parse_lease_s(payload)
        if lease_error is not None or lease_s is None:
            return json_response({"error": lease_error or "invalid lease_s"}, status=400)
        result = runtime.hijack.acquire(owner, lease_s)
        if not result.ok:
            return json_response({"error": result.error}, status=409)
        runtime.persist_lease(result.session)
        # Only send pause on a fresh acquisition; a same-owner renewal leaves the
        # worker already paused and a redundant pause frame could confuse workers
        # that track pause/resume counts or that need to acknowledge a new hijack_id.
        if not result.is_renewal:
            await runtime.push_worker_control("pause", owner=owner, lease_s=lease_s)
        await runtime.broadcast_hijack_state()
        return json_response(
            {
                "ok": True,
                "worker_id": runtime.worker_id,
                "hijack_id": result.session.hijack_id,
                "lease_expires_at": result.session.lease_expires_at,
                "owner": owner,
            }
        )

    if "/hijack/" in path and path.endswith("/heartbeat") and method == "POST":
        if await runtime.browser_role_for_request(request) != "admin":
            return json_response({"error": "admin role required"}, status=403)
        hijack_id = _extract_hijack_id(path)
        if not hijack_id:
            return json_response({"error": "not_found", "path": path}, status=404)
        payload = await runtime.request_json(request)
        lease_s, lease_error = _parse_lease_s(payload)
        if lease_error is not None or lease_s is None:
            return json_response({"error": lease_error or "invalid lease_s"}, status=400)
        result = runtime.hijack.heartbeat(hijack_id, lease_s)
        if not result.ok:
            return json_response({"error": result.error}, status=409)
        runtime.persist_lease(result.session)
        await runtime.broadcast_hijack_state()
        return json_response(
            {
                "ok": True,
                "worker_id": runtime.worker_id,
                "hijack_id": hijack_id,
                "lease_expires_at": result.session.lease_expires_at,
            }
        )

    if "/hijack/" in path and path.endswith("/release") and method == "POST":
        if await runtime.browser_role_for_request(request) != "admin":
            return json_response({"error": "admin role required"}, status=403)
        hijack_id = _extract_hijack_id(path)
        if not hijack_id:
            return json_response({"error": "not_found", "path": path}, status=404)
        result = runtime.hijack.release(hijack_id)
        if not result.ok:
            return json_response({"error": result.error}, status=409)
        runtime.clear_lease()
        await runtime.push_worker_control("resume", owner="release", lease_s=0)
        await runtime.broadcast_hijack_state()
        return json_response({"ok": True, "worker_id": runtime.worker_id, "hijack_id": hijack_id})

    if "/hijack/" in path and path.endswith("/step") and method == "POST":
        if await runtime.browser_role_for_request(request) != "admin":
            return json_response({"error": "admin role required"}, status=403)
        hijack_id = _extract_hijack_id(path)
        if not hijack_id:
            return json_response({"error": "not_found", "path": path}, status=404)
        if not runtime.hijack.can_send_input(hijack_id):
            return json_response({"error": "not_hijack_owner"}, status=403)
        owner = runtime.hijack.session.owner if runtime.hijack.session is not None else "unknown"
        ok = await runtime.push_worker_control("step", owner=owner, lease_s=0)
        if not ok:
            return json_response({"error": "no_worker"}, status=409)
        lease_expires_at = runtime.hijack.session.lease_expires_at if runtime.hijack.session is not None else None
        return json_response(
            {
                "ok": True,
                "worker_id": runtime.worker_id,
                "hijack_id": hijack_id,
                "lease_expires_at": lease_expires_at,
            }
        )

    if "/hijack/" in path and path.endswith("/send") and method == "POST":
        return await _handle_hijack_send(runtime, request, path)

    if "/hijack/" in path and path.endswith("/snapshot") and method == "GET":
        if await runtime.browser_role_for_request(request) != "admin":
            return json_response({"error": "admin role required"}, status=403)
        hijack_id = _extract_hijack_id(path)
        if not hijack_id:
            return json_response({"error": "not_found", "path": path}, status=404)
        # Prefer in-memory snapshot (most recent); fall back to store for DOs
        # that resumed after hibernation and have not yet received a new snapshot.
        snapshot: dict[str, object] | None = runtime.last_snapshot
        if snapshot is None:
            row = runtime.store.load_session(runtime.worker_id)
            snapshot = row.get("last_snapshot") if row else None
        session = runtime.hijack.session
        return json_response(
            build_hijack_snapshot_response(
                worker_id=runtime.worker_id,
                hijack_id=hijack_id,
                snapshot=snapshot,
                lease_expires_at=session.lease_expires_at if session is not None else None,
            )
        )

    if "/hijack/" in path and path.endswith("/events") and method == "GET":
        if await runtime.browser_role_for_request(request) != "admin":
            return json_response({"error": "admin role required"}, status=403)
        hijack_id = _extract_hijack_id(path)
        if not hijack_id:
            return json_response({"error": "not_found", "path": path}, status=404)
        session = runtime.hijack.session
        if session is None or session.hijack_id != hijack_id:
            return json_response({"error": "invalid or expired hijack session"}, status=404)
        qs = parse_qs(urlparse(url).query)
        after_seq = _safe_int(qs.get("after_seq", ["0"])[0], 0)
        limit = _safe_int(qs.get("limit", ["100"])[0], 100, min_val=1, max_val=500)
        rows = runtime.store.list_events_since(runtime.worker_id, after_seq, limit)
        latest_seq = runtime.store.current_event_seq(runtime.worker_id)
        min_event_seq = runtime.store.min_event_seq(runtime.worker_id)
        return json_response(
            build_hijack_events_response(
                worker_id=runtime.worker_id,
                hijack_id=hijack_id,
                after_seq=after_seq,
                latest_seq=latest_seq,
                min_event_seq=min_event_seq,
                events=rows,
                limit=limit,
                lease_expires_at=session.lease_expires_at,
            )
        )

    if path.endswith("/input_mode") and method == "POST":
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

    if path.endswith("/disconnect_worker") and method == "POST":
        if await runtime.browser_role_for_request(request) != "admin":
            return json_response({"error": "admin role required"}, status=403)
        if runtime.worker_ws is None:
            return json_response({"error": "No worker connected."}, status=404)
        with contextlib.suppress(Exception):
            runtime.worker_ws.close(1001, "disconnected by operator")
        return json_response({"ok": True, "worker_id": runtime.worker_id})

    return None


async def _handle_hijack_send(
    runtime: RuntimeProtocol,
    request: object,
    path: str,
) -> object:
    """Handle POST /hijack/{id}/send — validate, send input, optionally wait for prompt."""
    if await runtime.browser_role_for_request(request) != "admin":
        return json_response({"error": "admin role required"}, status=403)
    hijack_id = _extract_hijack_id(path)
    if not hijack_id:
        return json_response({"error": "not_found", "path": path}, status=404)
    payload = await runtime.request_json(request)
    data = str(payload.get("keys") or "")
    if not data:
        return json_response({"error": "keys must be non-empty"}, status=400)
    if len(data) > _MAX_INPUT_CHARS:
        return json_response({"error": "keys too long", "max": _MAX_INPUT_CHARS}, status=400)
    if not runtime.hijack.can_send_input(hijack_id):
        return json_response({"error": "not_hijack_owner"}, status=403)
    ok = await runtime.push_worker_input(data)
    if not ok:
        return json_response({"error": "no_worker"}, status=409)
    # Optional prompt guards — wait for screen to match before returning.
    expect_prompt_id = str(payload.get("expect_prompt_id") or "") or None
    expect_regex_raw = str(payload.get("expect_regex") or "") or None
    matched_snapshot = runtime.last_snapshot
    if expect_prompt_id or expect_regex_raw:
        # Pre-compile the regex before entering the poll loop so that a
        # malformed or pathological pattern raises re.error immediately,
        # enabling a clean 400 response (avoids ReDoS in the polling loop).
        expect_regex_obj: re.Pattern[str] | None = None
        if expect_regex_raw:
            try:
                expect_regex_obj = compile_expect_regex(expect_regex_raw, max_length=_MAX_REGEX_LEN)
            except PromptRegexError as exc:
                payload = {"error": str(exc)}
                if exc.kind == "too_long":
                    payload["max"] = int(exc.max_length or _MAX_REGEX_LEN)
                return json_response(payload, status=400)
        timeout_ms = _safe_int(payload.get("timeout_ms"), 5_000, min_val=100, max_val=_MAX_TIMEOUT_MS)
        poll_interval_ms = _safe_int(payload.get("poll_interval_ms"), 200, min_val=50, max_val=5_000)
        matched_snapshot = await _wait_for_prompt(
            runtime,
            expect_prompt_id=expect_prompt_id,
            expect_regex=expect_regex_obj,
            timeout_ms=timeout_ms,
            poll_interval_ms=poll_interval_ms,
        )
    session = runtime.hijack.session
    return json_response(
        {
            "ok": True,
            "worker_id": runtime.worker_id,
            "hijack_id": hijack_id,
            "sent": data,
            "matched_prompt_id": _extract_prompt_id(matched_snapshot),
            "lease_expires_at": session.lease_expires_at if session is not None else None,
        }
    )
