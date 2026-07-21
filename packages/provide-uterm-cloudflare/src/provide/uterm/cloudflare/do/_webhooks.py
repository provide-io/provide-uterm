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

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any, cast

try:
    from provide.uterm.cloudflare.do._webhook_crypto import decrypt_secret, encrypt_secret
except ImportError:  # pragma: no cover - CF flat bundle path
    from _webhook_crypto import (  # type: ignore[import-not-found,no-redef]  # ty:ignore[unresolved-import]
        decrypt_secret,
        encrypt_secret,
    )

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from provide.uterm.cloudflare.contracts import RuntimeProtocol

# Injected at module level in tests to avoid CF-only js.fetch dependency.
_outbound_fetch: Any = None

# Literal hosts / prefixes rejected as webhook destinations (SSRF floor).
_BLOCKED_WEBHOOK_HOSTS = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata",
    }
)
_BLOCKED_WEBHOOK_PREFIXES = (
    "127.",
    "10.",
    "192.168.",
    "169.254.",
    "0.",
    "100.64.",
    "100.100.100.",
)


def _validate_webhook_url(url: str) -> str | None:
    """Return an error message if *url* is not an allowed webhook destination.

    Requires ``https://`` and rejects obvious loopback / link-local / private
    / cloud-metadata hostnames. Full DNS-based egress is enforced server-side
    on FastAPI; CF edge applies this lightweight preflight only.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return "url is not a valid URL"
    if parsed.scheme.lower() != "https":
        return "url must use https"
    host = (parsed.hostname or "").lower().strip("[]")
    if not host:
        return "url host is required"
    if host in _BLOCKED_WEBHOOK_HOSTS:
        return "url host is not allowed"
    if host.startswith(_BLOCKED_WEBHOOK_PREFIXES):
        return "url host is not allowed"
    # IPv6 link-local / ULA
    if host == "::1" or host.startswith(("fe80:", "fc", "fd")):
        return "url host is not allowed"
    return None


# Cap the admin-supplied webhook screen-match pattern. fire_webhooks runs
# re.search(pattern, screen) on every snapshot; an unbounded or pathological
# pattern could burn the Durable Object's 50ms CPU budget. Bounding the length
# (and rejecting un-compilable patterns) at registration limits that, and the
# CF runtime's hard CPU ceiling backstops the rest.
_MAX_WEBHOOK_PATTERN_LEN = 256

# Per-delivery wall-clock cap. workerd exposes no fetch timeout we can rely on, so
# each POST is bounded by asyncio.wait_for. Combined with off-critical-path
# delivery (SessionRuntime._spawn_webhook_delivery), this keeps a slow or
# blackholed webhook URL from pinning a delivery task open indefinitely.
_WEBHOOK_TIMEOUT_S = 10.0


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
            import js  # type: ignore[import-not-found]  # CF flat path  # ty:ignore[unresolved-import]

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
        await asyncio.wait_for(
            fetch_fn(url, method="POST", headers=headers, body=body),
            timeout=_WEBHOOK_TIMEOUT_S,
        )
    except TimeoutError:
        logger.warning("webhook_delivery_timeout url=%s after=%ss", url, _WEBHOOK_TIMEOUT_S)
    except Exception as exc:
        logger.warning("webhook_delivery_error url=%s error=%s", url, exc)


async def fire_webhooks(
    runtime: RuntimeProtocol,
    event: dict[str, Any],
    *,
    _fetch: Any = None,
    _webhooks: list[dict[str, Any]] | None = None,
) -> None:
    """Fire all registered webhooks that match *event*.

    Scheduled off the broadcast critical path by
    ``SessionRuntime._spawn_webhook_delivery``. *_webhooks* lets that caller pass
    the already-loaded list so the store is not read twice per frame; when None
    the list is loaded here (the path direct callers/tests use).
    """
    webhooks = _webhooks if _webhooks is not None else runtime.store.load_webhooks(runtime.worker_id)
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
        stored_secret = wh.get("secret")
        secret = await decrypt_secret(getattr(runtime, "env", None), stored_secret) if stored_secret else None
        await _deliver_webhook(wh["url"], payload, secret, _fetch=_fetch)


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
    if TYPE_CHECKING:
        from provide.uterm.cloudflare.cf_types import json_response
    else:
        try:
            from provide.uterm.cloudflare.cf_types import json_response
        except ImportError:  # pragma: no cover
            from cf_types import (
                json_response,  # type: ignore[import-not-found,no-redef]  # CF flat path  # pragma: no cover
            )

    if session_id != runtime.worker_id:
        return json_response({"error": "not_found", "path": path}, status=404)

    # POST /api/sessions/{id}/webhooks — register
    if method == "POST" and webhook_id is None:
        payload = await runtime.request_json(request)
        hook_url = payload.get("url")
        if not hook_url or not isinstance(hook_url, str):
            return json_response({"error": "url is required"}, status=422)
        # HTTPS only; reject obvious SSRF destinations (metadata / loopback / private).
        url_err = _validate_webhook_url(hook_url)
        if url_err is not None:
            return json_response({"error": url_err}, status=422)
        event_types = payload.get("event_types")
        if event_types is not None and not isinstance(event_types, list):
            return json_response({"error": "event_types must be a list"}, status=422)
        pattern = payload.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str) or len(pattern) > _MAX_WEBHOOK_PATTERN_LEN:
                return json_response({"error": "pattern must be a string <= 256 chars"}, status=422)
            try:
                re.compile(pattern)
            except re.error:
                return json_response({"error": "pattern is not a valid regex"}, status=422)
        wh_id = uuid.uuid4().hex
        secret_raw = cast("str | None", payload.get("secret"))
        secret_stored = await encrypt_secret(getattr(runtime, "env", None), secret_raw) if secret_raw else None
        runtime.store.save_webhook(
            wh_id,
            session_id,
            hook_url,
            event_types=event_types,
            pattern=pattern,
            secret=secret_stored,
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
        webhooks = runtime.store.load_webhooks(session_id)
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
        webhooks = runtime.store.load_webhooks(session_id)
        if not any(wh["webhook_id"] == webhook_id for wh in webhooks):
            return json_response({"error": "not_found", "webhook_id": webhook_id}, status=404)
        runtime.store.delete_webhook(webhook_id)
        return json_response({"ok": True, "webhook_id": webhook_id})

    return json_response({"error": "not_found", "path": path}, status=404)
