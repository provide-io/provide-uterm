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

Mutation-enforced at killed==100 (see [tool.mutmut].paths_to_mutate). The bound
suite is tests/server/test_webhooks_mutation_killing.py, which mocks ALL egress
(socket.getaddrinfo + httpx + asyncio.sleep) so mutmut's forked workers never drive
the real C resolver/network — required because doing so segfaults the worker. It also
bounds the loop/cancel awaits with asyncio.wait_for so a mutant that breaks loop
termination fails fast instead of stalling the worker past its per-test timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
import socket
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from provide.telemetry import get_logger
from provide.uterm.server._net import _METADATA_IPS, _resolve_host
from provide.uterm.server.tracing import inject_trace_context

if TYPE_CHECKING:
    from provide.uterm.server.bridge.hub import EventBus

from provide.uterm.server.bridge.hub.event_bus import _compile_pattern

# ``verify_webhook_signature`` is re-exported (self-alias) for import compatibility:
# existing callers/tests import it from this module, though it now lives in
# ``webhook_signing``.
from provide.uterm.server.webhook_signing import (
    build_webhook_signature,
)
from provide.uterm.server.webhook_signing import (
    verify_webhook_signature as verify_webhook_signature,
)

logger = get_logger(__name__)

# Delivery retry settings
_MAX_RETRIES = 3
_RETRY_DELAYS = (0.5, 1.0, 2.0)
_DELIVER_TIMEOUT_S = 5.0
# Maximum consecutive SSRF-guard blocks tolerated before a webhook is
# automatically unregistered. Re-resolution of a previously-safe hostname can
# legitimately fail (e.g. DNS rebinding), but persistent failure means the
# webhook will never succeed and continuing to evaluate it just wastes work.
_MAX_BLOCKED_DELIVERIES = 3
Resolver = Callable[[str], Sequence[str] | Awaitable[Sequence[str]]]
# OnMetric type alias: callable(name, value=1) or None
_OnMetric = Callable[[str, int], None] | None

_REGISTER_DNS_TIMEOUT_S = 2.0


def _resolve_hostname_sync(hostname: str) -> tuple[str, ...]:
    """Resolve *hostname* to IP strings using the blocking stdlib resolver.

    A short global default timeout is applied so a hostile / slow DNS server
    can't stall webhook registration. Any error is re-raised as ``ValueError``
    by callers — DNS failures must be treated as "not allowed" rather than
    "allowed by default".
    """
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_REGISTER_DNS_TIMEOUT_S)
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    finally:
        socket.setdefaulttimeout(previous_timeout)
    return tuple({str(info[4][0]) for info in infos})


def validate_webhook_url(url: str, *, allow_loopback_destinations: bool = False) -> str:
    """Validate and normalize a webhook delivery URL.

    For DNS hostnames the resolver is queried with a short timeout and every
    returned address is checked against the deny list — this blocks DNS
    rebinding-style SSRF attempts where a name resolves to e.g. the cloud
    metadata IP. DNS failures are treated as "not allowed".
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:  # pragma: no cover — urlparse practically never raises on str input
        raise ValueError("webhook url is invalid") from exc

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("webhook url must use http or https")
    if not parsed.netloc or parsed.hostname is None:
        raise ValueError("webhook url must include a host")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "metadata.google.internal":
        raise ValueError("webhook url host is not allowed")
    if hostname in {"localhost"} or hostname.endswith(".localhost"):
        if allow_loopback_destinations:
            return url
        raise ValueError("webhook url host is not allowed")

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        # DNS hostname — resolve and ensure every address is safe.
        try:
            addresses = _resolve_hostname_sync(hostname)
        except (OSError, socket.gaierror, socket.herror, TimeoutError) as exc:
            raise ValueError("webhook url host could not be resolved") from exc
        if not addresses:
            raise ValueError("webhook url host could not be resolved") from None
        for address in addresses:
            try:
                if not _address_allowed(address, allow_loopback_destinations=allow_loopback_destinations):
                    raise ValueError("webhook url host is not allowed")
            except ValueError as exc:
                raise ValueError("webhook url host is not allowed") from exc
        return url

    if not _address_allowed(str(ip), allow_loopback_destinations=allow_loopback_destinations):
        raise ValueError("webhook url host is not allowed")

    return url


def validate_webhook_pattern(pattern: str | None) -> str | None:
    """Validate a webhook regex pattern."""
    if pattern is None:
        return None
    if not isinstance(pattern, str):
        raise ValueError("webhook pattern must be a string")
    try:
        _compile_pattern(pattern)
    except (re.error, ValueError) as exc:
        raise ValueError("webhook pattern is not a valid regex") from exc
    return pattern


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

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        allow_loopback_destinations: bool = False,
        on_metric: _OnMetric = None,
    ) -> None:
        self._webhooks: dict[str, WebhookConfig] = {}  # webhook_id → config
        self._tasks: dict[str, asyncio.Task[None]] = {}  # webhook_id → task
        self._resolver = resolver if resolver is not None else _resolve_host
        self._allow_loopback_destinations = bool(allow_loopback_destinations)
        self._on_metric = on_metric
        # Per-webhook count of consecutive deliveries blocked by the SSRF
        # guard. After ``_MAX_BLOCKED_DELIVERIES`` the webhook is auto-
        # unregistered to avoid burning CPU re-evaluating it forever.
        self._blocked_counts: dict[str, int] = {}
        # Strong refs to background unregister tasks so they aren't GC'd mid-flight.
        self._unregister_tasks: set[asyncio.Task[bool]] = set()

    def validate_url(self, url: str) -> str:
        return validate_webhook_url(url, allow_loopback_destinations=self._allow_loopback_destinations)

    @staticmethod
    def validate_pattern(pattern: str | None) -> str | None:
        return validate_webhook_pattern(pattern)

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
        url = self.validate_url(url)
        pattern = self.validate_pattern(pattern)
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
        self._blocked_counts.pop(webhook_id, None)
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
        self._blocked_counts.clear()

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
        if not await _delivery_url_allowed(
            cfg.url,
            self._resolver,
            allow_loopback_destinations=self._allow_loopback_destinations,
        ):
            self._blocked_counts[cfg.webhook_id] = self._blocked_counts.get(cfg.webhook_id, 0) + 1
            count = self._blocked_counts[cfg.webhook_id]
            self._on_metric and self._on_metric("webhook_delivery_blocked_total", 1)
            logger.warning(
                "webhook_delivery_blocked webhook_id=%s url=%s reason=unsafe_destination count=%d",
                cfg.webhook_id,
                cfg.url,
                count,
            )
            if count >= _MAX_BLOCKED_DELIVERIES:
                self._on_metric and self._on_metric("webhook_auto_unregistered_total", 1)
                logger.error(
                    "webhook_auto_unregistered webhook_id=%s url=%s reason=ssrf_guard_threshold count=%d",
                    cfg.webhook_id,
                    cfg.url,
                    count,
                )
                # Schedule unregister in the background so we don't try to
                # await our own delivery task (which is what calls us).
                unreg_task = asyncio.create_task(self.unregister(cfg.webhook_id))
                self._unregister_tasks.add(unreg_task)
                unreg_task.add_done_callback(self._unregister_tasks.discard)
            return
        # Successful guard pass — reset the consecutive-block counter so a
        # webhook that's been intermittently safe-then-unsafe-then-safe isn't
        # killed by a stale count.
        self._blocked_counts.pop(cfg.webhook_id, None)

        payload = {
            "webhook_id": cfg.webhook_id,
            "session_id": cfg.session_id,
            "event": event,
            "timestamp": time.time(),
        }
        body = json.dumps(payload).encode()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if cfg.secret:
            ts = str(time.time())
            headers["X-Uterm-Timestamp"] = ts
            headers["X-Uterm-Signature"] = build_webhook_signature(cfg.secret, body, ts)
        # Propagate the active W3C trace context onto the delivery so the
        # downstream webhook receiver joins the same distributed trace. Via
        # provide.telemetry (OpenTelemetry-optional) — no-op when no span active.
        inject_trace_context(headers)

        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                async with httpx.AsyncClient(timeout=_DELIVER_TIMEOUT_S) as http:
                    resp = await http.post(cfg.url, content=body, headers=headers)
                if resp.is_success:
                    return
                self._on_metric and self._on_metric("webhook_delivery_failed_total", 1)
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
        self._on_metric and self._on_metric("webhook_delivery_giving_up_total", 1)
        logger.error(
            "webhook_delivery_giving_up webhook_id=%s url=%s",
            cfg.webhook_id,
            cfg.url,
        )


async def _delivery_url_allowed(
    url: str,
    resolver: Resolver,
    *,
    allow_loopback_destinations: bool = False,
) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:  # pragma: no cover — urlparse practically never raises on str input
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.hostname
    if host is None:
        return False

    try:
        addresses_result = _literal_or_resolved_addresses(host, resolver)
        if isinstance(addresses_result, Awaitable):
            resolved_addresses = await addresses_result
        else:
            resolved_addresses = addresses_result
    except Exception:
        return False
    addresses = tuple(resolved_addresses)
    if not addresses:
        return False
    try:
        return all(
            _address_allowed(address, allow_loopback_destinations=allow_loopback_destinations) for address in addresses
        )
    except (
        ValueError
    ):  # pragma: no cover — _address_allowed only raises on malformed IP strings the resolver wouldn't return
        return False


def _literal_or_resolved_addresses(host: str, resolver: Resolver) -> Sequence[str] | Awaitable[Sequence[str]]:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return resolver(host)
    return (host,)


def _address_allowed(address: str, *, allow_loopback_destinations: bool = False) -> bool:
    # NOTE on embedded-IPv4 IPv6 forms (mapped / 6to4 / NAT64 / compat): the
    # egress connector guard decodes these explicitly (egress._decode_embedded_ipv4),
    # but here the stdlib's is_private / is_reserved / is_link_local already
    # classify every such form carrying a metadata or private IPv4 as blocked on
    # all supported interpreters (verified on 3.11 + 3.13 — see the
    # test_embedded_ipv4_* regression tests, which pin the invariant and will
    # fail loudly if a future CPython ever relaxes that classification). No
    # explicit decode is added so this guard stays mutation-clean rather than
    # carrying redundant, untestable-on-supported-Pythons code.
    ip = ipaddress.ip_address(address)
    if ip in _METADATA_IPS:
        return False
    if ip.is_loopback:
        return allow_loopback_destinations
    # ``is_reserved`` covers IANA-reserved ranges (Class E, benchmarking
    # 198.18/15, documentation 198.51.100/24 + 203.0.113/24 on IPv6 — IPv4
    # documentation ranges are caught by ``is_reserved`` in Python's stdlib
    # via the IANA special-purpose registry).
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved
    )
