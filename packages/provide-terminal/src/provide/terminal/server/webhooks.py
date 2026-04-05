#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Webhook delivery manager for the hosted terminal server.

Webhooks are registered per session and backed by background EventBus
subscribers.  When an event arrives it is POSTed to the configured URL with
an optional HMAC-SHA256 signature.

Usage::

    manager = WebhookManager()
    cfg = await manager.register("s1", "https://example.com/hook",
                                 event_types=["snapshot"], secret="mysecret")
    # background delivery starts immediately
    await manager.shutdown()  # cancels all delivery tasks
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from provide.telemetry import get_logger

if TYPE_CHECKING:
    from provide.terminal.bridge.hub import EventBus

logger = get_logger(__name__)

# Delivery retry settings
_MAX_RETRIES = 3
_RETRY_DELAYS = (0.5, 1.0, 2.0)
_DELIVER_TIMEOUT_S = 5.0


@dataclass
class WebhookConfig:
    """Configuration for a single registered webhook."""

    webhook_id: str
    session_id: str
    url: str
    event_types: frozenset[str] | None  # None = all types
    pattern: str | None
    secret: str | None  # HMAC-SHA256 signing key; None = unsigned


class WebhookManager:
    """In-memory webhook registry with background EventBus delivery tasks.

    One background task per registered webhook subscribes to the EventBus
    and POSTs matching events to the webhook URL.  Tasks are cancelled when
    the webhook is unregistered or when :meth:`shutdown` is called.
    """

    def __init__(self) -> None:
        self._webhooks: dict[str, WebhookConfig] = {}  # webhook_id → config
        self._tasks: dict[str, asyncio.Task[None]] = {}  # webhook_id → task

    async def register(
        self,
        session_id: str,
        url: str,
        *,
        event_types: list[str] | None = None,
        pattern: str | None = None,
        secret: str | None = None,
        event_bus: EventBus | None = None,
    ) -> WebhookConfig:
        """Register a webhook and start its background delivery task.

        Args:
            session_id: Session to subscribe to.
            url: URL to POST events to.
            event_types: Only deliver these event types.  ``None`` = all.
            pattern: Regex filter on ``snapshot`` events' ``data.screen``.
            secret: HMAC-SHA256 signing key.  ``None`` = no signature.
            event_bus: EventBus instance to subscribe to.  When ``None``
                the delivery task exits immediately (graceful no-op).
        """
        cfg = WebhookConfig(
            webhook_id=uuid.uuid4().hex,
            session_id=session_id,
            url=url,
            event_types=frozenset(event_types) if event_types is not None else None,
            pattern=pattern,
            secret=secret,
        )
        self._webhooks[cfg.webhook_id] = cfg
        task = asyncio.create_task(self._delivery_loop(cfg, event_bus))
        task.add_done_callback(
            lambda t: (
                logger.error(
                    "webhook_delivery_loop_failed webhook_id=%s error=%s",
                    cfg.webhook_id,
                    t.exception(),
                )
                if not t.cancelled() and t.exception() is not None
                else None
            )
        )
        self._tasks[cfg.webhook_id] = task
        return cfg

    async def unregister(self, webhook_id: str) -> bool:
        """Cancel and remove a webhook by ID.  Returns True if found."""
        cfg = self._webhooks.pop(webhook_id, None)
        if cfg is None:
            return False
        task = self._tasks.pop(webhook_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return True

    def list_webhooks(self, session_id: str) -> list[WebhookConfig]:
        """Return all registered webhooks for *session_id*."""
        return [cfg for cfg in self._webhooks.values() if cfg.session_id == session_id]

    def get_webhook(self, webhook_id: str) -> WebhookConfig | None:
        return self._webhooks.get(webhook_id)

    async def shutdown(self) -> None:
        """Cancel all delivery tasks and clear the registry."""
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._webhooks.clear()

    # ------------------------------------------------------------------
    # Delivery internals
    # ------------------------------------------------------------------

    async def _delivery_loop(self, cfg: WebhookConfig, event_bus: EventBus | None) -> None:
        """Background task: subscribe to EventBus and POST each event."""
        if event_bus is None:
            return

        event_types = list(cfg.event_types) if cfg.event_types is not None else None
        async with event_bus.watch(
            cfg.session_id,
            event_types=event_types,
            pattern=cfg.pattern,
        ) as sub:
            while True:
                try:
                    item = await sub.queue.get()
                except asyncio.CancelledError:
                    return
                if item is None:  # worker disconnected sentinel
                    return
                await self._deliver(cfg, item)

    async def _deliver(self, cfg: WebhookConfig, event: dict[str, Any]) -> None:
        """POST *event* to *cfg.url* with retries."""
        payload = {
            "webhook_id": cfg.webhook_id,
            "session_id": cfg.session_id,
            "event": event,
            "timestamp": time.time(),
        }
        body = json.dumps(payload).encode()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if cfg.secret:
            sig = hmac.new(cfg.secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Uterm-Signature"] = f"sha256={sig}"

        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                async with httpx.AsyncClient(timeout=_DELIVER_TIMEOUT_S) as http:
                    resp = await http.post(cfg.url, content=body, headers=headers)
                if resp.is_success:
                    return
                logger.warning(
                    "webhook_delivery_failed webhook_id=%s url=%s status=%d attempt=%d",
                    cfg.webhook_id,
                    cfg.url,
                    resp.status_code,
                    attempt + 1,
                )
            except Exception as exc:
                logger.warning(
                    "webhook_delivery_error webhook_id=%s url=%s error=%s attempt=%d",
                    cfg.webhook_id,
                    cfg.url,
                    exc,
                    attempt + 1,
                )
            if delay is not None:
                await asyncio.sleep(delay)
        logger.error(
            "webhook_delivery_giving_up webhook_id=%s url=%s",
            cfg.webhook_id,
            cfg.url,
        )
