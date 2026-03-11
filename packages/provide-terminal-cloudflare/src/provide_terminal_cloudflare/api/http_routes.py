from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

try:
    from provide_terminal_cloudflare.cf_types import Response, json_response
except Exception:  # pragma: no cover
    from cf_types import Response, json_response  # type: ignore[import-not-found]  # pragma: no cover

if TYPE_CHECKING:
    try:
        from provide_terminal_cloudflare.contracts import RuntimeProtocol
    except Exception:
        from contracts import RuntimeProtocol  # type: ignore[import-not-found]

# Matches /hijack/{hijack_id}/ in any path segment position.
_HIJACK_ID_RE = re.compile(r"/hijack/([0-9a-fA-F\-]{1,64})/")
_MIN_LEASE_S = 1
_MAX_LEASE_S = 3600
_MAX_INPUT_CHARS = 10_000  # must match main package TermHub.max_input_chars default
_SESSION_ROUTE_RE = re.compile(r"^/api/sessions/([a-zA-Z0-9_-]{1,64})(?:/([a-z]+))?$")
_MAX_TIMEOUT_MS = 30_000
_MAX_PROMPT_POLL_S = 30.0


def _extract_hijack_id(path: str) -> str | None:
    m = _HIJACK_ID_RE.search(path)
    return m.group(1) if m else None


def _parse_lease_s(payload: dict[str, object], *, default: int = 60) -> tuple[int | None, str | None]:
    value = payload.get("lease_s", default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, "lease_s must be an integer"
    return max(_MIN_LEASE_S, min(parsed, _MAX_LEASE_S)), None


def _extract_prompt_id(snapshot: dict[str, object] | None) -> str | None:
    if not snapshot:
        return None
    prompt = snapshot.get("prompt_detected")
    if isinstance(prompt, dict):
        value = prompt.get("prompt_id")
        if isinstance(value, str) and value:
            return value
    return None


async def _wait_for_prompt(
    runtime: RuntimeProtocol,
    *,
    expect_prompt_id: str | None,
    expect_regex: str | None,
    timeout_ms: int,
    poll_interval_ms: int,
) -> dict[str, object] | None:
    """Poll last_snapshot until a prompt guard matches or the timeout expires."""
    timeout_s = max(0.1, min(timeout_ms / 1000, _MAX_PROMPT_POLL_S))
    interval_s = max(0.05, min(poll_interval_ms / 1000, 5.0))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snapshot = runtime.last_snapshot
        if snapshot:
            if expect_prompt_id and _extract_prompt_id(snapshot) == expect_prompt_id:
                return snapshot
            if expect_regex and re.search(expect_regex, str(snapshot.get("screen", ""))):
                return snapshot
        await asyncio.sleep(interval_s)
    return runtime.last_snapshot


async def _wait_for_analysis(runtime: RuntimeProtocol, *, timeout_ms: int = 5_000) -> str | None:
    """Poll last_analysis until a result arrives or the timeout expires."""
    timeout_s = max(0.1, min(timeout_ms / 1000, _MAX_PROMPT_POLL_S))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if runtime.last_analysis:
            return runtime.last_analysis
        await asyncio.sleep(0.2)
    return runtime.last_analysis


def _session_status_item(runtime: RuntimeProtocol) -> dict[str, object]:
    """Build a SessionStatus-compatible dict from the current DO state."""
    connected = runtime.worker_ws is not None
    return {
        "session_id": runtime.worker_id,
        "display_name": runtime.worker_id,
        "connector_type": "unknown",
        "lifecycle_state": "running" if connected else "idle",
        "input_mode": runtime.input_mode,
        "connected": connected,
        "auto_start": False,
        "tags": [],
        "recording_enabled": False,
        "recording_available": False,
        "owner": None,
        "visibility": "public",
        "last_error": None,
        "hijacked": runtime.hijack.session is not None,
    }


async def route_http(runtime: RuntimeProtocol, request: object) -> Response:
    url = str(getattr(request, "url", ""))
    path = urlparse(url).path
    method = str(getattr(request, "method", "GET")).upper()

    if path == "/api/health":
        return json_response({"ok": True, "service": "provide-terminal-cloudflare"})

    if path == "/api/sessions":
        return json_response([_session_status_item(runtime)], headers={"X-Sessions-Scope": "local"})

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
        expect_regex = str(payload.get("expect_regex") or "") or None
        matched_snapshot = runtime.last_snapshot
        if expect_prompt_id or expect_regex:
            try:
                timeout_ms = max(100, min(int(payload.get("timeout_ms") or 5_000), _MAX_TIMEOUT_MS))
                poll_interval_ms = max(50, min(int(payload.get("poll_interval_ms") or 200), 5_000))
            except (TypeError, ValueError):
                timeout_ms, poll_interval_ms = 5_000, 200
            matched_snapshot = await _wait_for_prompt(
                runtime,
                expect_prompt_id=expect_prompt_id,
                expect_regex=expect_regex,
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
            {
                "ok": True,
                "worker_id": runtime.worker_id,
                "hijack_id": hijack_id,
                "snapshot": snapshot,
                "prompt_id": _extract_prompt_id(snapshot),
                "lease_expires_at": session.lease_expires_at if session is not None else None,
            }
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
        try:
            after_seq = int(parse_qs(urlparse(url).query).get("after_seq", ["0"])[0])
        except (ValueError, IndexError):
            after_seq = 0
        try:
            limit = max(1, min(int(parse_qs(urlparse(url).query).get("limit", ["100"])[0]), 500))
        except (ValueError, IndexError):
            limit = 100
        rows = runtime.store.list_events_since(runtime.worker_id, after_seq, limit)
        latest_seq = runtime.store.current_event_seq(runtime.worker_id)
        min_event_seq = runtime.store.min_event_seq(runtime.worker_id)
        return json_response(
            {
                "ok": True,
                "worker_id": runtime.worker_id,
                "hijack_id": hijack_id,
                "after_seq": after_seq,
                "latest_seq": latest_seq,
                "min_event_seq": min_event_seq,
                "has_more": len(rows) == limit,
                "events": rows,
                "lease_expires_at": session.lease_expires_at,
            }
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
        runtime.input_mode = mode  # type: ignore[misc]
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

    # --------------------------------------------------------------------------
    # /api/sessions/{id}[/sub] — per-session status, snapshot, events, control
    # --------------------------------------------------------------------------
    session_match = _SESSION_ROUTE_RE.match(path)
    if session_match:
        session_id, sub = session_match.group(1), (session_match.group(2) or "")
        if session_id != runtime.worker_id:
            return json_response({"error": "not_found", "path": path}, status=404)

        if sub == "" and method == "GET":
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
            try:
                after_seq = int(qs.get("after_seq", ["0"])[0])
            except (ValueError, IndexError):
                after_seq = 0
            try:
                limit = max(1, min(int(qs.get("limit", ["100"])[0]), 500))
            except (ValueError, IndexError):
                limit = 100
            rows = runtime.store.list_events_since(runtime.worker_id, after_seq, limit)
            latest_seq = runtime.store.current_event_seq(runtime.worker_id)
            min_event_seq = runtime.store.min_event_seq(runtime.worker_id)
            return json_response(
                {
                    "session_id": runtime.worker_id,
                    "after_seq": after_seq,
                    "latest_seq": latest_seq,
                    "min_event_seq": min_event_seq,
                    "has_more": len(rows) == limit,
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
            runtime.input_mode = mode  # type: ignore[misc]
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

        return json_response({"error": "not_found", "path": path}, status=404)

    return json_response({"error": "not_found", "path": path}, status=404)
