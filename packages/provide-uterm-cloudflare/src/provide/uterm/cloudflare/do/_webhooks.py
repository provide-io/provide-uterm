#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Webhook delivery routes and mixin for the CF Durable Object session runtime.

Webhooks are stored in the DO's SQLite store.  When an event arrives (via
``broadcast_worker_frame``), ``_fire_webhooks()`` loads all registered webhooks
for the session and fires outbound ``fetch()`` requests for each matching one.

Routes:
  POST   /api/sessions/{id}/webhooks          — register
  GET    /api/sessions/{id}/webhooks          — list
  DELETE /api/sessions/{id}/webhooks/{wh_id}  — unregister

Outbound delivery is fire-and-forget: CF DOs cannot hold async tasks between
requests, so a failed delivery is silently dropped (no retry queue).  The
caller should use the FastAPI package for reliable webhook delivery if needed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from provide.uterm.cloudflare.contracts import RuntimeProtocol

# Injected at module level in tests to avoid CF-only js.fetch dependency.
_outbound_fetch: Any = None


async def _deliver_webhook(
    url: str,
    payload: dict[str, Any],
    secret: str | None,
    *,
    _fetch: Any = None,
) -> None:
    """POST *payload* to *url*.  Uses *_fetch* if provided, else ``js.fetch``."""
    fetch_fn = _fetch or _outbound_fetch
    if fetch_fn is None:
        try:
            import js  # type: ignore[import-not-found]  # CF flat path

            fetch_fn = js.fetch  # pragma: no cover
        except ImportError:
            logger.debug("outbound fetch unavailable — skipping webhook delivery")
            return

    body = json.dumps(payload, ensure_ascii=True)
    headers: dict[str, str] = {"content-type": "application/json"}
    if secret:
        sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        headers["x-uterm-signature"] = f"sha256={sig}"

    try:
        await fetch_fn(
            url,
            method="POST",
            headers=headers,
            body=body,
        )
    except Exception as exc:
        logger.warning("webhook_delivery_error url=%s error=%s", url, exc)


async def fire_webhooks(
    runtime: RuntimeProtocol,
    event: dict[str, Any],
    *,
    _fetch: Any = None,
) -> None:
    """Fire all registered webhooks that match *event*.

    Called from ``broadcast_worker_frame`` after storing the event.
    """
    webhooks = runtime.store.load_webhooks(runtime.worker_id)
    event_type = str(event.get("type") or "")
    screen = str(event.get("screen") or event.get("data", {}).get("screen") or "")

    for wh in webhooks:
        # event_types filter
        et = wh.get("event_types")
        if et is not None and event_type not in et:
            continue
        # pattern filter (only on snapshot events with a screen field)
        pat = wh.get("pattern")
        if pat and event_type == "snapshot":
            try:
                if not re.search(pat, screen):
                    continue
            except re.error:
                continue

        payload = {
            "webhook_id": wh["webhook_id"],
            "session_id": runtime.worker_id,
            "event": event,
            "timestamp": time.time(),
        }
        await _deliver_webhook(wh["url"], payload, wh.get("secret"), _fetch=_fetch)


async def route_webhooks(
    runtime: RuntimeProtocol,
    request: object,
    path: str,
    _url: str,
    method: str,
    session_id: str,
    webhook_id: str | None = None,
) -> object:
    """Handle /api/sessions/{id}/webhooks[/{webhook_id}] routes."""
    try:
        from provide.uterm.cloudflare.cf_types import json_response
    except ImportError:  # pragma: no cover
        from cf_types import json_response  # type: ignore[import-not-found]  # CF flat path  # pragma: no cover

    if session_id != runtime.worker_id:
        return json_response({"error": "not_found", "path": path}, status=404)

    # POST /api/sessions/{id}/webhooks — register
    if method == "POST" and webhook_id is None:
        payload = await runtime.request_json(request)  # type: ignore[attr-defined]
        hook_url = payload.get("url")
        if not hook_url or not isinstance(hook_url, str):
            return json_response({"error": "url is required"}, status=422)
        event_types = payload.get("event_types")
        if event_types is not None and not isinstance(event_types, list):
            return json_response({"error": "event_types must be a list"}, status=422)
        wh_id = uuid.uuid4().hex
        runtime.store.save_webhook(  # type: ignore[attr-defined]
            wh_id,
            session_id,
            hook_url,
            event_types=event_types,
            pattern=payload.get("pattern"),
            secret=payload.get("secret"),
        )
        return json_response(
            {
                "webhook_id": wh_id,
                "session_id": session_id,
                "url": hook_url,
                "event_types": event_types,
                "pattern": payload.get("pattern"),
            }
        )

    # GET /api/sessions/{id}/webhooks — list
    if method == "GET" and webhook_id is None:
        webhooks = runtime.store.load_webhooks(session_id)  # type: ignore[attr-defined]
        return json_response(
            {
                "webhooks": [
                    {
                        "webhook_id": wh["webhook_id"],
                        "session_id": wh["session_id"],
                        "url": wh["url"],
                        "event_types": wh["event_types"],
                        "pattern": wh["pattern"],
                    }
                    for wh in webhooks
                ]
            }
        )

    # DELETE /api/sessions/{id}/webhooks/{webhook_id}
    if method == "DELETE" and webhook_id is not None:
        # Verify it belongs to this session before deleting.
        webhooks = runtime.store.load_webhooks(session_id)  # type: ignore[attr-defined]
        if not any(wh["webhook_id"] == webhook_id for wh in webhooks):
            return json_response({"error": "not_found", "webhook_id": webhook_id}, status=404)
        runtime.store.delete_webhook(webhook_id)  # type: ignore[attr-defined]
        return json_response({"ok": True, "webhook_id": webhook_id})

    return json_response({"error": "not_found", "path": path}, status=404)
