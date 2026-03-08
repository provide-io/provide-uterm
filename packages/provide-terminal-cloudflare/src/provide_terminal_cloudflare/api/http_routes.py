from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

try:
    from provide_terminal_cloudflare.cf_types import Response, json_response
except Exception:
    from cf_types import Response, json_response

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


async def route_http(runtime: object, request: object) -> Response:
    url = str(getattr(request, "url", ""))
    path = urlparse(url).path
    method = str(getattr(request, "method", "GET")).upper()

    if path == "/api/health":
        return json_response({"ok": True, "service": "provide-terminal-cloudflare"})

    if path == "/api/sessions":
        return json_response(
            {
                "sessions": [
                    {
                        "session_id": runtime.worker_id,
                        "connected": runtime.worker_ws is not None,
                        "hijacked": runtime.hijack.session is not None,
                    }
                ]
            }
        )

    if path.endswith("/hijack/acquire") and method == "POST":
        # Hijack requires admin role.
        if runtime.browser_role_for_request(request) != "admin":
            return json_response({"error": "admin role required"}, status=403)
        payload = await runtime.request_json(request)
        owner = str(payload.get("owner") or "unknown")
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
            {"hijack_id": result.session.hijack_id, "lease_expires_at": result.session.lease_expires_at}
        )

    if "/hijack/" in path and path.endswith("/heartbeat") and method == "POST":
        if runtime.browser_role_for_request(request) != "admin":
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
        return json_response({"lease_expires_at": result.session.lease_expires_at})

    if "/hijack/" in path and path.endswith("/release") and method == "POST":
        if runtime.browser_role_for_request(request) != "admin":
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
        return json_response({"released": True})

    if "/hijack/" in path and path.endswith("/step") and method == "POST":
        if runtime.browser_role_for_request(request) != "admin":
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
        return json_response({"stepped": True})

    if "/hijack/" in path and path.endswith("/send") and method == "POST":
        if runtime.browser_role_for_request(request) != "admin":
            return json_response({"error": "admin role required"}, status=403)
        hijack_id = _extract_hijack_id(path)
        if not hijack_id:
            return json_response({"error": "not_found", "path": path}, status=404)
        payload = await runtime.request_json(request)
        data = str(payload.get("keys") or "")
        if not runtime.hijack.can_send_input(hijack_id):
            return json_response({"error": "not_hijack_owner"}, status=403)
        ok = await runtime.push_worker_input(data)
        if not ok:
            return json_response({"error": "no_worker"}, status=409)
        return json_response({"sent": True})

    if "/hijack/" in path and path.endswith("/events") and method == "GET":
        try:
            seq = int(parse_qs(urlparse(url).query).get("since", ["0"])[0])
        except (ValueError, IndexError):
            seq = 0
        return json_response({"events": runtime.store.list_events_since(runtime.worker_id, seq)})

    return json_response({"error": "not_found", "path": path}, status=404)
