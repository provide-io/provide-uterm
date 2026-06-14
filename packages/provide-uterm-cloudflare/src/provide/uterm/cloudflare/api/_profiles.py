#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Connection profiles CRUD — stored in KV (``profile:{id}`` keys).

Routes handled by entry.py (global, not per-session DO):
  GET    /api/profiles              — list own + shared profiles
  GET    /api/profiles/{id}         — get one profile
  POST   /api/profiles              — create profile
  PUT    /api/profiles/{id}         — update mutable fields
  DELETE /api/profiles/{id}         — delete profile
  POST   /api/profiles/{id}/connect — create session from profile
"""

from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from provide.uterm.cloudflare.cf_types import json_response
else:
    try:
        from provide.uterm.cloudflare.cf_types import json_response
    except ImportError:  # pragma: no cover
        from cf_types import json_response  # type: ignore[import-not-found,no-redef]

_MUTABLE_FIELDS = frozenset(
    {"name", "host", "port", "username", "tags", "input_mode", "recording_enabled", "visibility"}
)
_CONNECTOR_TYPES = frozenset({"ssh", "telnet", "websocket", "ushell", "shell"})


async def route_profiles(
    request: object,
    env: object,
    path: str,
    method: str,
    principal_id: str,
) -> object:
    """Dispatch /api/profiles routes.  *principal_id* is the authenticated user."""
    kv = getattr(env, "SESSION_REGISTRY", None)
    if kv is None:
        return json_response({"error": "SESSION_REGISTRY not configured"}, status=500)

    # POST /api/profiles (create)
    if path == "/api/profiles" and method == "POST":
        return await _create(request, kv, principal_id)

    # GET /api/profiles (list)
    if path == "/api/profiles" and method == "GET":
        return await _list(kv, principal_id)

    # /api/profiles/{id}[/connect]
    parts = path.removeprefix("/api/profiles/").split("/", 1)
    pid = parts[0] if parts else ""
    sub = parts[1] if len(parts) > 1 else ""

    if not pid:
        return json_response({"error": "not_found"}, status=404)

    if sub == "connect" and method == "POST":
        return await _connect(request, env, kv, pid, principal_id)
    if sub == "" and method == "GET":
        return await _get(kv, pid, principal_id)
    if sub == "" and method == "PUT":
        return await _update(request, kv, pid, principal_id)
    if sub == "" and method == "DELETE":
        return await _delete(kv, pid, principal_id)

    return json_response({"error": "not_found"}, status=404)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _kv_get_profile(kv: object, pid: str) -> dict[str, Any] | None:
    raw = await kv.get(f"profile:{pid}")  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
    if raw is None:
        return None
    decoded: dict[str, Any] = json.loads(str(raw) if isinstance(raw, str) else raw)
    return decoded


async def _kv_put_profile(kv: object, profile: dict[str, Any]) -> None:
    await kv.put(f"profile:{profile['profile_id']}", json.dumps(profile, ensure_ascii=True))  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]


def _can_access(profile: dict[str, Any], principal_id: str) -> bool:
    return profile.get("owner") == principal_id or profile.get("visibility") == "shared"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _list(kv: object, principal_id: str) -> object:
    keys_result = await kv.list(prefix="profile:")  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
    keys = [k.get("name") or k for k in (getattr(keys_result, "keys", None) or keys_result or [])]
    profiles = []
    for key in keys:
        raw = await kv.get(str(key))  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
        if not raw:
            continue
        try:
            p = json.loads(str(raw) if isinstance(raw, str) else raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if _can_access(p, principal_id):
            profiles.append(p)
    return json_response(profiles)


async def _get(kv: object, pid: str, principal_id: str) -> object:
    p = await _kv_get_profile(kv, pid)
    if p is None:
        return json_response({"detail": f"unknown profile: {pid}"}, status=404)
    if not _can_access(p, principal_id):
        return json_response({"detail": "insufficient privileges"}, status=403)
    return json_response(p)


async def _create(request: object, kv: object, principal_id: str) -> object:
    try:
        raw = await request.json()  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
        body = raw.to_py() if hasattr(raw, "to_py") else raw
    except Exception:
        body = {}
    now = time.time()
    ct = str(body.get("connector_type") or "ssh")
    if ct not in _CONNECTOR_TYPES:
        return json_response({"detail": f"invalid connector_type: {ct}"}, status=422)
    profile = {
        "profile_id": f"profile-{uuid.uuid4().hex[:12]}",
        "owner": principal_id,
        "name": str(body.get("name") or "Unnamed"),
        "connector_type": ct,
        "host": body.get("host"),
        "port": body.get("port"),
        "username": body.get("username"),
        "tags": list(body.get("tags") or []),
        "input_mode": str(body.get("input_mode") or "open"),
        "recording_enabled": bool(body.get("recording_enabled")),
        "visibility": str(body.get("visibility") or "private"),
        "created_at": now,
        "updated_at": now,
    }
    await _kv_put_profile(kv, profile)
    return json_response(profile)


async def _update(request: object, kv: object, pid: str, principal_id: str) -> object:
    p = await _kv_get_profile(kv, pid)
    if p is None:
        return json_response({"detail": f"unknown profile: {pid}"}, status=404)
    if p.get("owner") != principal_id:
        return json_response({"detail": "insufficient privileges"}, status=403)
    try:
        raw = await request.json()  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
        body = raw.to_py() if hasattr(raw, "to_py") else raw
    except Exception:
        body = {}
    for key in _MUTABLE_FIELDS:
        if key in body:
            p[key] = body[key]
    p["updated_at"] = time.time()
    await _kv_put_profile(kv, p)
    return json_response(p)


async def _delete(kv: object, pid: str, principal_id: str) -> object:
    p = await _kv_get_profile(kv, pid)
    if p is None:
        return json_response({"detail": f"unknown profile: {pid}"}, status=404)
    if p.get("owner") != principal_id:
        return json_response({"detail": "insufficient privileges"}, status=403)
    await kv.delete(f"profile:{pid}")  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
    return json_response({"ok": True})


async def _connect(
    request: object,
    env: object,
    kv: object,
    pid: str,
    principal_id: str,
) -> object:
    p = await _kv_get_profile(kv, pid)
    if p is None:
        return json_response({"detail": f"unknown profile: {pid}"}, status=404)
    if not _can_access(p, principal_id):
        return json_response({"detail": "insufficient privileges"}, status=403)
    try:
        raw = await request.json()  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
        raw.to_py() if hasattr(raw, "to_py") else raw
    except Exception:
        pass
    # Build session entry (same shape as /api/connect)
    import json as _json

    ct = str(p.get("connector_type") or "shell")
    prefix = "ushell" if ct == "ushell" else "connect"
    session_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
    connector_config: dict[str, Any] = {}
    if p.get("host"):
        connector_config["host"] = p["host"]
    if p.get("port"):
        connector_config["port"] = p["port"]
    if p.get("username"):
        connector_config["username"] = p["username"]
    # Password is intentionally excluded from KV storage (credentials must not be persisted).
    # The DO runtime receives the password through the initial worker connection handshake.
    entry = {
        "session_id": session_id,
        "display_name": str(p.get("name") or session_id),
        "created_at": time.time(),
        "connector_type": ct,
        "connector_config": connector_config,
        "lifecycle_state": "waiting",
        "input_mode": str(p.get("input_mode") or "open"),
        "connected": False,
        "auto_start": False,
        "tags": list(p.get("tags") or []),
        "recording_enabled": bool(p.get("recording_enabled")),
        "recording_available": False,
        "owner": principal_id,
        "visibility": "private",
        "last_error": None,
    }
    session_kv = getattr(env, "SESSION_REGISTRY", None)
    if session_kv is None:
        return json_response({"error": "SESSION_REGISTRY not configured"}, status=500)
    await session_kv.put(f"session:{session_id}", _json.dumps({**entry, "hijacked": False}))
    return json_response({**entry, "url": f"/app/session/{session_id}"})
