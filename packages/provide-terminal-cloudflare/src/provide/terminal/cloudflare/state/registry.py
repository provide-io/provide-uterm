"""Fleet-wide session registry backed by Cloudflare KV.

Each Durable Object instance writes its session status to KV under the key
``session:{worker_id}``.  The Default Worker reads the full list from KV to
serve ``GET /api/sessions`` with fleet-wide scope.

KV is eventually consistent; the data returned by list/get may be up to ~60s
stale for the global network, but <1s for requests routed to the same colo.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_KV_PREFIX = "session:"
_KV_EXPIRATION_TTL = 300  # 5 min safety-net TTL; alarm() heartbeat keeps it fresh during active leases
KV_REFRESH_S = 60  # alarm reschedule interval for KV heartbeat (exported)


async def update_kv_session(
    env: Any,
    worker_id: str,
    *,
    connected: bool,
    hijacked: bool = False,
    input_mode: str = "hijack",
    recording_enabled: bool = True,
    recording_available: bool = False,
    meta: dict[str, Any] | None = None,
) -> None:
    """Write (or delete) this DO's session entry in the KV registry.

    Safe to call from a DO — does nothing if the KV binding is absent (e.g.
    in unit tests or when the registry feature is not configured).
    """
    kv = getattr(env, "SESSION_REGISTRY", None)
    if kv is None:
        return
    key = f"{_KV_PREFIX}{worker_id}"
    if not connected:
        try:
            await kv.delete(key)
        except Exception as exc:
            logger.debug("kv delete %s failed: %s", key, exc)
        return
    m = meta or {}
    status: dict[str, Any] = {
        "session_id": worker_id,
        "display_name": m.get("display_name") or worker_id,
        "created_at": m.get("created_at") or 0.0,
        "connector_type": m.get("connector_type") or "unknown",
        "lifecycle_state": "running",
        "input_mode": input_mode,
        "connected": True,
        "auto_start": False,
        "tags": m.get("tags") or [],
        "recording_enabled": recording_enabled,
        "recording_available": recording_available,
        "owner": m.get("owner"),
        "visibility": m.get("visibility") or "public",
        "last_error": None,
        "hijacked": hijacked,
    }
    try:
        # Note: do NOT pass expirationTtl as a keyword argument — CF Python Workers
        # (Pyodide) cannot map Python kwargs to the JS options object for KV.put().
        # Entries are cleaned up explicitly on disconnect via kv.delete().
        await kv.put(key, json.dumps(status, ensure_ascii=True))
    except Exception as exc:
        logger.debug("kv put %s failed: %s", key, exc)


async def delete_kv_session(env: Any, worker_id: str) -> None:
    """Delete a session entry from the KV registry.

    Safe to call from the Default Worker.  Does nothing if the KV binding is
    absent or the key does not exist.
    """
    kv = getattr(env, "SESSION_REGISTRY", None)
    if kv is None:
        return
    key = f"{_KV_PREFIX}{worker_id}"
    try:
        await kv.delete(key)
    except Exception as exc:
        logger.debug("kv delete %s failed: %s", key, exc)


async def list_kv_sessions(env: Any) -> list[dict[str, Any]]:
    """Return all session entries from the KV registry.

    Returns an empty list if the KV binding is not configured.
    """
    kv = getattr(env, "SESSION_REGISTRY", None)
    if kv is None:
        return []
    try:
        result = await kv.list(prefix=_KV_PREFIX)
        keys: list[dict[str, Any]] = result.keys if hasattr(result, "keys") else result.get("keys", [])
    except Exception as exc:
        logger.warning("kv list failed: %s", exc)
        return []

    sessions: list[dict[str, Any]] = []
    for key_info in keys:
        key_name = key_info.get("name") if isinstance(key_info, dict) else getattr(key_info, "name", None)
        if not key_name:
            continue
        try:
            raw = await kv.get(key_name)
            if raw:
                sessions.append(json.loads(raw))
        except Exception as exc:
            logger.debug("kv get %s failed: %s", key_name, exc)
    return sessions
