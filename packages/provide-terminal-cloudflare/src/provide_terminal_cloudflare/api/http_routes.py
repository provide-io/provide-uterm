from __future__ import annotations

from urllib.parse import urlparse

try:
    from provide_terminal_cloudflare.cf_types import Response, json_response
except Exception:
    from cf_types import Response, json_response


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
        payload = await runtime.request_json(request)
        owner = str(payload.get("owner") or "unknown")
        lease_s = int(payload.get("lease_s") or 60)
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
        hijack_id = path.rsplit("/", 2)[1]
        payload = await runtime.request_json(request)
        lease_s = int(payload.get("lease_s") or 60)
        result = runtime.hijack.heartbeat(hijack_id, lease_s)
        if not result.ok:
            return json_response({"error": result.error}, status=409)
        runtime.persist_lease(result.session)
        await runtime.broadcast_hijack_state()
        return json_response({"lease_expires_at": result.session.lease_expires_at})

    if "/hijack/" in path and path.endswith("/release") and method == "POST":
        hijack_id = path.rsplit("/", 2)[1]
        result = runtime.hijack.release(hijack_id)
        if not result.ok:
            return json_response({"error": result.error}, status=409)
        runtime.clear_lease()
        await runtime.push_worker_control("resume", owner="release", lease_s=0)
        await runtime.broadcast_hijack_state()
        return json_response({"released": True})

    if "/hijack/" in path and path.endswith("/send") and method == "POST":
        hijack_id = path.rsplit("/", 2)[1]
        payload = await runtime.request_json(request)
        data = str(payload.get("keys") or "")
        if not runtime.hijack.can_send_input(hijack_id):
            return json_response({"error": "not_hijack_owner"}, status=403)
        ok = await runtime.push_worker_input(data)
        if not ok:
            return json_response({"error": "no_worker"}, status=409)
        return json_response({"sent": True})

    if "/hijack/" in path and path.endswith("/events") and method == "GET":
        seq_raw = urlparse(url).query
        seq = 0
        if "since=" in seq_raw:
            try:
                seq = int(seq_raw.split("since=", 1)[1].split("&", 1)[0])
            except Exception:
                seq = 0
        return json_response({"events": runtime.store.list_events_since(runtime.worker_id, seq)})

    return json_response({"error": "not_found", "path": path}, status=404)
