#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from provide.uterm.api_routes import API_ROUTES, RouteDef, RouteRegistry, RouteScope
from provide.uterm.cloudflare.state.registry import update_kv_session

from ._recording import route_recording
from ._shared import (
    _extract_prompt_id,
    _safe_int,
    _session_status_item,
    _wait_for_analysis,
)

try:
    from provide.uterm.cloudflare.do._sse import route_sse
    from provide.uterm.cloudflare.do._webhooks import route_webhooks
except ImportError:  # pragma: no cover — CF flat-path fallback
    from do._sse import route_sse  # type: ignore[import-not-found,no-redef]  # ty:ignore[unresolved-import]
    from do._webhooks import route_webhooks  # type: ignore[import-not-found,no-redef]  # ty:ignore[unresolved-import]

if TYPE_CHECKING:
    from provide.uterm.cloudflare.cf_types import json_response
else:
    try:
        from provide.uterm.cloudflare.cf_types import json_response
    except ImportError:  # pragma: no cover
        from cf_types import (
            json_response,  # type: ignore[import-not-found,no-redef]  # CF flat path  # pragma: no cover
        )

if TYPE_CHECKING:
    from provide.uterm.cloudflare.contracts import RuntimeProtocol


SessionHandler = Callable[..., Awaitable[object]]
SESSION_ROUTE_REGISTRY = RouteRegistry(tuple(route for route in API_ROUTES if route.scope is RouteScope.SESSION))


async def _can_mutate_session(runtime: RuntimeProtocol, request: object) -> bool:
    """Return True when the caller may mutate the current session.

    Cloudflare browser roles are broader than the FastAPI mutation policy, so
    we require either an admin caller or the session owner. This keeps
    operator visibility useful for reads without turning it into a blanket
    mutation grant.
    """
    if await runtime.browser_role_for_request(request) == "admin":
        return True
    subject = await runtime.browser_subject_for_request(request)
    owner = runtime.meta.get("owner")
    return subject is not None and subject == owner


async def _get_session(
    runtime: RuntimeProtocol,
    _request: object,
    _path: str,
    _url: str,
    _route: RouteDef,
    _params: Mapping[str, str],
) -> object:
    return json_response(_session_status_item(runtime))


async def _snapshot(
    runtime: RuntimeProtocol, _request: object, _path: str, _url: str, _route: RouteDef, _params: Mapping[str, str]
) -> object:
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


async def _events(
    runtime: RuntimeProtocol, _request: object, _path: str, url: str, _route: RouteDef, _params: Mapping[str, str]
) -> object:
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


async def _events_watch(
    runtime: RuntimeProtocol, _request: object, _path: str, url: str, _route: RouteDef, _params: Mapping[str, str]
) -> object:
    """Return an immediate event batch; DO hibernation cannot safely long-poll."""
    qs = parse_qs(urlparse(url).query)
    max_events = _safe_int(qs.get("max_events", ["50"])[0], 50, min_val=1, max_val=200)
    events = runtime.store.list_events_since(runtime.worker_id, 0, max_events)
    event_types = {item for item in qs.get("event_types", [""])[0].split(",") if item}
    if event_types:
        events = [event for event in events if event.get("type") in event_types]
    pattern = qs.get("pattern", [None])[0]
    if pattern:
        try:
            regex = re.compile(pattern)
        except re.error:
            return json_response({"error": "invalid pattern"}, status=422)
        events = [event for event in events if regex.search(str(event))]
    return json_response({"events": events, "dropped_count": 0, "timed_out": False})


async def _set_mode(
    runtime: RuntimeProtocol, request: object, _path: str, _url: str, _route: RouteDef, _params: Mapping[str, str]
) -> object:
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
    await runtime.broadcast_hijack_state()
    return json_response({"ok": True, "input_mode": mode, "worker_id": runtime.worker_id})


async def _clear(
    runtime: RuntimeProtocol, request: object, _path: str, _url: str, _route: RouteDef, _params: Mapping[str, str]
) -> object:
    if not await _can_mutate_session(runtime, request):
        return json_response({"error": "owner or admin role required"}, status=403)
    runtime.last_snapshot = None
    if runtime.worker_ws is not None:
        await runtime.send_ws(runtime.worker_ws, {"type": "snapshot_req", "ts": time.monotonic()})
    return json_response(_session_status_item(runtime))


async def _analyze(
    runtime: RuntimeProtocol, request: object, _path: str, _url: str, _route: RouteDef, _params: Mapping[str, str]
) -> object:
    if not await _can_mutate_session(runtime, request):
        return json_response({"error": "owner or admin role required"}, status=403)
    if not await runtime.push_worker_control("analyze", owner="", lease_s=0):
        return json_response({"error": "no_worker"}, status=409)
    analysis = await _wait_for_analysis(runtime, timeout_ms=5_000)
    return json_response({"ok": True, "analysis": analysis, "worker_id": runtime.worker_id})


async def _delete(
    runtime: RuntimeProtocol, request: object, _path: str, _url: str, _route: RouteDef, _params: Mapping[str, str]
) -> object:
    if not await _can_mutate_session(runtime, request):
        return json_response({"error": "owner or admin role required"}, status=403)
    runtime.lifecycle_state = "deleted"
    runtime._deleted_at = time.time()  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
    with contextlib.suppress(Exception):
        runtime.store.mark_deleted(runtime.worker_id)
    sockets = [
        runtime.worker_ws,
        *getattr(runtime, "browser_sockets", {}).values(),
        *getattr(runtime, "raw_sockets", {}).values(),
    ]
    for sock in sockets:
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.close(1001, "session deleted")  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
    return json_response({"ok": True, "session_id": runtime.worker_id, "deleted": True})


async def _restart(
    runtime: RuntimeProtocol, request: object, _path: str, _url: str, _route: RouteDef, _params: Mapping[str, str]
) -> object:
    if not await _can_mutate_session(runtime, request):
        return json_response({"error": "owner or admin role required"}, status=403)
    runtime.last_snapshot = None
    if runtime.worker_ws is not None:
        with contextlib.suppress(Exception):
            runtime.worker_ws.close(1001, "restart requested")
    return json_response({**_session_status_item(runtime), "restarted": True})


async def _connect(
    runtime: RuntimeProtocol, request: object, _path: str, _url: str, _route: RouteDef, _params: Mapping[str, str]
) -> object:
    if not await _can_mutate_session(runtime, request):
        return json_response({"error": "owner or admin role required"}, status=403)
    # A Durable Object cannot start an external connector process. Its ushell
    # connector starts only on browser attachment, so neither case is a
    # successful replacement for FastAPI's start_session().
    if runtime.worker_ws is None and not (
        getattr(runtime, "_ushell", None) is not None and bool(getattr(runtime, "_ushell_started", False))
    ):
        return json_response({"error": "no_worker"}, status=409)
    runtime.lifecycle_state = "running"
    return json_response(_session_status_item(runtime))


async def _disconnect(
    runtime: RuntimeProtocol, request: object, _path: str, _url: str, _route: RouteDef, _params: Mapping[str, str]
) -> object:
    if not await _can_mutate_session(runtime, request):
        return json_response({"error": "owner or admin role required"}, status=403)
    worker_ws = runtime.worker_ws
    if worker_ws is not None:
        with contextlib.suppress(Exception):
            worker_ws.close(1001, "disconnect requested")
        runtime.worker_ws = None
    ushell = getattr(runtime, "_ushell", None)
    if ushell is not None and bool(getattr(runtime, "_ushell_started", False)):
        with contextlib.suppress(Exception):
            await ushell.stop()
        runtime._ushell_started = False
    runtime.lifecycle_state = "stopped"
    await update_kv_session(
        getattr(runtime, "env", None),
        runtime.worker_id,
        connected=False,
        remove_offline=False,
        hijacked=runtime.hijack.session is not None,
        input_mode=runtime.input_mode,
        recording_available=runtime.store.current_event_seq(runtime.worker_id) > 0,
        meta=runtime.meta,
    )
    return json_response(_session_status_item(runtime))


async def _update(
    runtime: RuntimeProtocol, request: object, _path: str, _url: str, _route: RouteDef, _params: Mapping[str, str]
) -> object:
    if not await _can_mutate_session(runtime, request):
        return json_response({"error": "owner or admin role required"}, status=403)
    payload = await runtime.request_json(request)
    supported = frozenset({"display_name", "tags", "visibility"})
    unsupported = sorted(set(payload) - supported)
    if not payload or unsupported:
        return json_response({"error": "unsupported session update fields", "fields": unsupported}, status=422)
    next_meta = dict(runtime.meta)
    if "display_name" in payload:
        display_name = payload["display_name"]
        if not isinstance(display_name, str) or not display_name.strip():
            return json_response({"error": "display_name must be a non-empty string"}, status=422)
        next_meta["display_name"] = display_name.strip()
    if "tags" in payload:
        tags = payload["tags"]
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            return json_response({"error": "tags must be a list of strings"}, status=422)
        next_meta["tags"] = tags
    if "visibility" in payload:
        visibility = payload["visibility"]
        if visibility not in {"public", "operator", "private"}:
            return json_response({"error": "visibility must be public, operator, or private"}, status=422)
        next_meta["visibility"] = visibility
    runtime.meta = next_meta
    runtime.store.save_session_meta(runtime.worker_id, runtime.meta)
    await update_kv_session(
        runtime.env,
        runtime.worker_id,
        connected=None,
        hijacked=runtime.hijack.session is not None,
        input_mode=runtime.input_mode,
        recording_available=runtime.store.current_event_seq(runtime.worker_id) > 0,
        meta=runtime.meta,
    )
    return json_response(_session_status_item(runtime))


async def _annotate(
    runtime: RuntimeProtocol, request: object, _path: str, _url: str, _route: RouteDef, _params: Mapping[str, str]
) -> object:
    if not await _can_mutate_session(runtime, request):
        return json_response({"error": "owner or admin role required"}, status=403)
    payload = await runtime.request_json(request)
    label = str(payload.get("label", "")).strip()
    if not label:
        return json_response({"error": "label is required"}, status=400)
    severity = str(payload.get("severity", "info"))
    if severity not in {"info", "warning", "high", "critical"}:
        return json_response({"error": f"invalid severity: {severity}"}, status=400)
    event = {
        "type": "annotation",
        "label": label,
        "description": str(payload.get("description", "")),
        "severity": severity,
        "ts": time.time(),
    }
    await runtime.broadcast_worker_frame(event)
    return json_response({"ts": event["ts"], "seq": runtime.store.current_event_seq(runtime.worker_id)})


async def _events_stream(
    runtime: RuntimeProtocol, request: object, _path: str, url: str, _route: RouteDef, _params: Mapping[str, str]
) -> object:
    return await route_sse(runtime, request, url, runtime.worker_id)


async def _recording(
    runtime: RuntimeProtocol, request: object, _path: str, url: str, route: RouteDef, _params: Mapping[str, str]
) -> object:
    suffix = route.operation.removeprefix("sessions.recording")
    sub = suffix.removeprefix("_") or None
    return await route_recording(runtime, request, url, runtime.worker_id, sub)


async def _webhooks(
    runtime: RuntimeProtocol, request: object, path: str, url: str, route: RouteDef, params: Mapping[str, str]
) -> object:
    return await route_webhooks(
        runtime, request, path, url, route.method.value, runtime.worker_id, params.get("webhook_id")
    )


SESSION_CAPABILITIES: dict[str, SessionHandler] = {
    "sessions.get": _get_session,
    "sessions.update": _update,
    "sessions.delete": _delete,
    "sessions.connect": _connect,
    "sessions.disconnect": _disconnect,
    "sessions.restart": _restart,
    "sessions.set_mode": _set_mode,
    "sessions.clear": _clear,
    "sessions.annotate": _annotate,
    "sessions.analyze": _analyze,
    "sessions.snapshot": _snapshot,
    "sessions.events": _events,
    "sessions.events_watch": _events_watch,
    "sessions.events_stream": _events_stream,
    "sessions.recording": _recording,
    "sessions.recording_entries": _recording,
    "sessions.recording_download": _recording,
    "sessions.webhooks.create": _webhooks,
    "sessions.webhooks.list": _webhooks,
    "sessions.webhooks.delete": _webhooks,
}


def _validate_session_capabilities() -> None:
    """Ensure the Durable Object owns every shared session capability only."""
    SESSION_ROUTE_REGISTRY.validate_capabilities(SESSION_CAPABILITIES)
    global_capabilities = {route.capability for route in API_ROUTES if route.scope is RouteScope.GLOBAL}
    if global_capabilities & set(SESSION_CAPABILITIES):
        msg = "global RouteDef capability registered in Durable Object"
        raise ValueError(msg)


_validate_session_capabilities()
