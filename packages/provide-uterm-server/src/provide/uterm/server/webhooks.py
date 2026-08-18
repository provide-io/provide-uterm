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

Two destination guards run, both fail-closed:

  * ``validate_webhook_url`` at registration; and
  * ``_delivery_url_allowed`` again at every delivery (re-resolving DNS, so a
    rebind after registration is caught), followed by
    ``_refuse_loopback_while_tunnel_shared``, which withdraws the loopback
    permission for as long as the session has a live tunnel share. That order —
    destination safety first, share second — is fixed by
    ``conformance/EGRESS_GUARD.md`` §4; see ``_deliver``.

Loopback is the only refusal any config can relax, and the effective loopback
permission is computed by the app factory (config key OR loopback bind) — see
``validate_webhook_url`` and ``app/factory_impl.py``.

Mutation-enforced at killed==100 (see [tool.mutmut].source_paths). The bound
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
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from provide.telemetry import get_logger
from provide.uterm.server import _http
from provide.uterm.server._net import _CGNAT_V4, _METADATA_IPS, _resolve_host
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

    ``allow_loopback_destinations`` is the *effective* permission, not the raw
    config key: the app factory ORs ``webhooks.allow_loopback_destinations``
    with "the server is bound to loopback" before it reaches this module (see
    ``app/factory_impl.py``). Loopback is the ONLY refusal this flag relaxes —
    private, link-local, multicast, unspecified, reserved and cloud-metadata
    destinations are refused unconditionally and there is deliberately no
    config key that re-opens any of them.
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
        tunnel_tokens: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        """Build a webhook registry.

        Args:
            resolver: DNS resolver for delivery-time re-validation.
            allow_loopback_destinations: The *effective* loopback permission
                (config key OR loopback bind — computed by the app factory).
            on_metric: Counter sink, ``callable(name, value)``.
            tunnel_tokens: A **live reference** to the server's tunnel-share
                token store (``app.state.uterm_tunnel_tokens``) — the same
                object the tunnel routes mutate, deliberately not a copy, so
                shares created/revoked after construction are visible. Used
                only by the delivery-time share guard (see ``_deliver``).
                ``None`` disables that guard, for embedders and unit tests that
                have no tunnel state. Same injection idiom as
                ``SessionRegistry(tunnel_tokens=...)``.
        """
        self._webhooks: dict[str, WebhookConfig] = {}  # webhook_id → config
        self._tasks: dict[str, asyncio.Task[None]] = {}  # webhook_id → task
        self._resolver = resolver if resolver is not None else _resolve_host
        self._allow_loopback_destinations = bool(allow_loopback_destinations)
        self._on_metric = on_metric
        self._tunnel_tokens = tunnel_tokens
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

    async def _refuse_loopback_while_tunnel_shared(self, cfg: WebhookConfig) -> bool:
        """Refuse (and count) a loopback delivery while this session is shared.

        A loopback destination is permitted at all only because "the server is
        bound to loopback" was read as "only local callers exist" (see
        ``validate_webhook_url``). An active tunnel share falsifies that: the
        share relays this loopback-bound server to remote viewers through a
        public relay, so a loopback webhook destination becomes a
        reachable-from-outside SSRF pivot into whatever else happens to be
        listening on 127.0.0.1. Shares are issued at runtime
        (``POST /api/tunnels``), so this can only be decided per delivery — it
        cannot be folded into the load-time default.

        Refused regardless of the effective permission: an explicit
        ``webhooks.allow_loopback_destinations = true`` is an operator's
        judgement about a *non-shared* server, and must not survive the server
        being shared.

        Deliberately does NOT touch ``_blocked_counts``: a share retires itself
        on expiry, so feeding its refusals into the ``_MAX_BLOCKED_DELIVERIES``
        kill switch would let a few minutes of sharing silently delete a
        perfectly healthy webhook. It gets its own counter for the same reason —
        an operator needs to tell "suppressed while you are sharing" apart from
        "this destination has gone bad".

        Runs AFTER destination safety (see ``_deliver``), which is what makes the
        exemption above safe: by the time this is reached the destination is
        already known to be deliverable under the current configuration, so
        exempting it from the kill switch cannot keep a permanently-dead webhook
        alive.

        Returns True when the delivery was refused (caller must stop).
        """
        if not _tunnel_share_active(self._tunnel_tokens, cfg.session_id, now=time.time()):
            return False
        if not await _delivery_url_is_loopback_only(cfg.url, self._resolver):
            return False
        self._on_metric and self._on_metric("webhook_delivery_blocked_tunnel_total", 1)
        logger.warning(
            "webhook_delivery_blocked webhook_id=%s url=%s session_id=%s "
            "reason=loopback_destination_while_tunnel_shared",
            cfg.webhook_id,
            cfg.url,
            cfg.session_id,
        )
        return True

    async def _deliver(self, cfg: WebhookConfig, event: dict[str, Any]) -> None:
        """POST *event* to *cfg.url* with retries.

        The two delivery guards run in a fixed order: destination safety FIRST,
        the tunnel share SECOND (``conformance/EGRESS_GUARD.md`` §4, and matching
        the Go and C# ports).

        The orders differ in exactly one state — the configuration refuses
        loopback, a share is live, and the destination is loopback-only. Such a
        destination can never deliver under the current configuration, so it
        belongs on the generic counter, which eventually retires the webhook via
        ``_MAX_BLOCKED_DELIVERIES``. The share guard exists for destinations that
        would otherwise be fine, and it is deliberately exempt from that kill
        switch; booking a permanently-dead webhook there would keep it alive
        forever, re-evaluated on every event for as long as the process runs.
        """
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
        # killed by a stale count. Reset here, before the share guard, because
        # the count belongs to the guard that just passed; a share refusal is
        # not evidence that the destination has gone bad.
        self._blocked_counts.pop(cfg.webhook_id, None)

        if await self._refuse_loopback_while_tunnel_shared(cfg):
            return

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
                async with _http.async_client(timeout=_DELIVER_TIMEOUT_S) as http:
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


def _tunnel_share_active(
    tunnel_tokens: Mapping[str, Mapping[str, object]] | None,
    session_id: str,
    *,
    now: float,
) -> bool:
    """True when *session_id* currently has a live (unexpired) tunnel share.

    The share store is keyed by session id — ``POST /api/tunnels`` creates a
    session whose id *is* the tunnel id — so presence in the store is the whole
    question, plus expiry.

    Expiry is re-checked here rather than trusted to the sweeper: that sweep
    (``sweep_expired_tunnel_tokens``) only ticks once a minute, so a lapsed
    share lingers in the store for up to 60s. Reading presence alone would let
    one short share keep the webhook guard shut long after the share stopped
    exposing anything.

    ``now == expires_at`` counts as **expired** here. That is deliberately the
    opposite of the ``now > expires_at`` convention in
    ``sweep_expired_tunnel_tokens`` / ``consume_tunnel_invite``, and it is fixed
    by the cross-port contract (``conformance/EGRESS_GUARD.md`` §4) so all four
    ports agree on the boundary. The instant is unobservable in practice (float
    clock equality); parity is the point.

    A record whose ``expires_at`` is missing or non-numeric cannot be *proven*
    lapsed, so it is treated as live (fail closed — refuse the webhook). The
    store is typed ``Mapping[str, object]``, so this narrowing is required in
    any case; both writers (``create_tunnel``/``rotate_tunnel_tokens``) always
    store a float.
    """
    if tunnel_tokens is None:
        return False
    state = tunnel_tokens.get(session_id)
    if state is None:
        return False
    expires_at = state.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return True
    return now < float(expires_at)


async def _delivery_url_is_loopback_only(url: str, resolver: Resolver) -> bool:
    """True when *url*'s destination is loopback.

    Answered by asking the existing delivery guard with loopback refused: a
    destination it turns down under that setting is a loopback one. Reusing the
    guard rather than re-implementing parse -> resolve -> classify keeps exactly
    one answer to "what does this URL actually reach" — including hostnames that
    resolve to 127.0.0.1, and every embedded-IPv4 loopback form — and adds no
    second classifier that could drift from the first.

    Only ever called after destination safety has already passed (see
    ``_deliver``), and that ordering is what makes the single question
    sufficient. It used to ask twice, refusing then permitting loopback, to tell
    "loopback and otherwise fine" apart from "unsafe for some other reason";
    with safety checked first, "some other reason" can no longer be standing
    here, so the second call could only ever answer True. It was removed rather
    than kept as a defensive re-check: an always-true branch is untestable, and
    an untestable branch is one nothing would notice going wrong.
    """
    return not await _delivery_url_allowed(url, resolver, allow_loopback_destinations=False)


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
    if not isinstance(resolved_addresses, Iterable):
        return False
    addresses = tuple(str(address) for address in resolved_addresses)
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
    # RFC 6598 carrier-grade NAT (100.64.0.0/10). Checked explicitly because the
    # rest of this function derives its deny list from CPython's classifiers and
    # CPython does not consider CGNAT private (see the _CGNAT_V4 comment in
    # _net.py) — so without this line the whole /10 is silently permitted.
    # Unconditional, deliberately placed above the loopback branch so it reads as
    # what it is: a member of the always-refused set, not of the one conditional
    # case. Loopback is the ONLY refusal a config key relaxes, and CGNAT is not
    # loopback. (conformance/EGRESS_GUARD.md §1)
    if ip in _CGNAT_V4:
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
