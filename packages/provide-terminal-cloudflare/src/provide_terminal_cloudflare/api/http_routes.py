from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

try:
    from provide_terminal_cloudflare.cf_types import Response, json_response
except Exception:
    from cf_types import Response, json_response  # type: ignore[import-not-found]

if TYPE_CHECKING:
    try:
        from provide_terminal_cloudflare.contracts import RuntimeProtocol
    except Exception:
        from contracts import RuntimeProtocol  # type: ignore[import-not-found]

# Matches /hijack/{hijack_id}/ in any path segment position.
_HIJACK_ID_RE = re.compile(r"/hijack/([0-9a-fA-F\-]{1,64})/")
_MIN_LEASE_S = 1
_MAX_LEASE_S = 3600


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


async def route_http(runtime: RuntimeProtocol, request: object) -> Response:
    url = str(getattr(request, "url", ""))
    path = urlparse(url).path
    method = str(getattr(request, "method", "GET")).upper()

    if path == "/api/health":
        return json_response({"ok": True, "service": "provide-terminal-cloudflare"})

    if path == "/api/sessions":
        connected = runtime.worker_ws is not None
        item: dict[str, object] = {
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
            # CF-specific field — FastAPI clients must tolerate extra keys.
            "hijacked": runtime.hijack.session is not None,
        }
        return json_response([item], headers={"X-Sessions-Scope": "local"})

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
        if not runtime.hijack.can_send_input(hijack_id):
            return json_response({"error": "not_hijack_owner"}, status=403)
        ok = await runtime.push_worker_input(data)
        if not ok:
            return json_response({"error": "no_worker"}, status=409)
        session = runtime.hijack.session
        return json_response(
            {
                "ok": True,
                "worker_id": runtime.worker_id,
                "hijack_id": hijack_id,
                "sent": data,
                "matched_prompt_id": _extract_prompt_id(runtime.last_snapshot),
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
        hijack_id = _extract_hijack_id(path)
        if not hijack_id:
            return json_response({"error": "not_found", "path": path}, status=404)
        try:
            after_seq = int(parse_qs(urlparse(url).query).get("after_seq", ["0"])[0])
        except (ValueError, IndexError):
            after_seq = 0
        limit = 100
        rows = runtime.store.list_events_since(runtime.worker_id, after_seq, limit)
        latest_seq = runtime.store.current_event_seq(runtime.worker_id)
        min_event_seq = runtime.store.min_event_seq(runtime.worker_id)
        session = runtime.hijack.session
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
                "lease_expires_at": session.lease_expires_at if session is not None else None,
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

    return json_response({"error": "not_found", "path": path}, status=404)
