#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
r"""Connection lifecycle mixin for TermHub.

Extracted from \`\`core.py\`\` to keep file sizes under 500 LOC.
Provides public methods used by WS route handlers to register and
deregister workers/browsers without accessing hub internals directly.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger, get_tracer
from provide.uterm.bridge.hub.ext import (
    EVENT_RATE_LIMIT_TRIGGERED,
    EVENT_SESSION_DISCONNECTED,
    EVENT_SESSION_REGISTERED,
)
from provide.uterm.bridge.hub.limiter import (
    REST_CLIENT_CACHE_MAX as _REST_CLIENT_CACHE_MAX,
)
from provide.uterm.bridge.hub.limiter import (
    REST_CLIENT_EVICT_COUNT as _REST_CLIENT_EVICT_COUNT,
)
from provide.uterm.bridge.hub.limiter import (
    RateLimiter,
)
from provide.uterm.bridge.models import WorkerTermState

if TYPE_CHECKING:
    from provide.uterm.bridge.contracts import InputMode
    from provide.uterm.bridge.hub.resume import ResumeTokenStore
    from provide.uterm.bridge.ratelimit import TokenBucket

logger = get_logger(__name__)
tracer = get_tracer(__name__)


async def shutdown_background_tasks(task_set: set[asyncio.Task[Any]]) -> int:
    """Cancel and await all pending background tasks. Returns count cancelled."""
    tasks = list(task_set)
    if not tasks:
        return 0
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    task_set.clear()
    return sum(1 for r in results if isinstance(r, (asyncio.CancelledError, Exception)))


# Re-exported from :mod:`provide.uterm.bridge.hub.limiter` so existing
# call sites that import these from ``connections`` keep working. The
# canonical definitions live in the limiter module now.
__all__ = ["_REST_CLIENT_CACHE_MAX", "_REST_CLIENT_EVICT_COUNT"]

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import WebSocket


class _ConnectionMixin:
    r"""Mixin providing worker/browser connection lifecycle methods for TermHub.

    Requires the host class to provide: \`\`_lock\`\`, \`\`_workers\`\`,
    \`\`_worker_token\`\`, \`\`is_hijacked\`\`, \`\`is_dashboard_hijack_active\`\`,
    \`\`has_valid_rest_lease\`\`, \`\`send_worker\`\`, \`\`broadcast_hijack_state\`\`,
    \`\`notify_hijack_changed\`\`, \`\`_resolve_role_for_browser\`\`.
    """

    # -- Declared attributes required from the host class ----------------------
    _lock: asyncio.Lock
    _workers: dict[str, WorkerTermState]
    _worker_token: str | None
    _event_deque_maxlen: int
    limiter: RateLimiter
    _rest_acquire_bucket: TokenBucket
    _rest_send_bucket: TokenBucket
    _rest_acquire_per_client: dict[str, TokenBucket]
    _rest_send_per_client: dict[str, TokenBucket]
    _background_tasks: set[asyncio.Task[Any]]
    _resume_store: ResumeTokenStore | None
    _resume_ttl_s: float
    _ws_to_resume_token: dict[Any, str]
    _startup_pending_browsers: set[Any]

    # Methods provided by TermHub / _HijackOwnershipMixin used in this mixin.
    is_hijacked: Callable[..., bool]
    is_dashboard_hijack_active: Callable[..., bool]
    has_valid_rest_lease: Callable[..., bool]
    send_worker: Callable[..., Awaitable[bool]]
    broadcast_hijack_state: Callable[..., Awaitable[None]]
    notify_hijack_changed: Callable[..., None]
    _resolve_role_for_browser: Callable[..., Awaitable[str]]

    # --------------------------------------------------------------------------

    # -- Rate limiting ---------------------------------------------------------

    def allow_rest_acquire_for(self, client_id: str) -> bool:
        r"""Per-client REST acquire rate limit (also checks the global bucket).

        Delegates the bucket composition and LRU-lite eviction to
        :attr:`limiter`; only the structured-event log on a rejection
        lives here so the hub keeps a single observability surface.
        """
        allowed = self.limiter.allow_rest_acquire(client_id)
        if not allowed:
            logger.warning(EVENT_RATE_LIMIT_TRIGGERED, client_id=client_id, limit_type="rest_acquire")
        return allowed

    def allow_rest_send_for(self, client_id: str) -> bool:
        r"""Per-client REST send/step rate limit (also checks the global bucket).

        Delegates the bucket composition and LRU-lite eviction to
        :attr:`limiter` (same strategy as :meth:`allow_rest_acquire_for`).
        """
        allowed = self.limiter.allow_rest_send(client_id)
        if not allowed:
            logger.warning(EVENT_RATE_LIMIT_TRIGGERED, client_id=client_id, limit_type="rest_send")
        return allowed

    # -- Token access ----------------------------------------------------------

    def worker_token(self) -> str | None:
        """Return the configured worker bearer token (read-only)."""
        return self._worker_token

    # -- Worker connection lifecycle --------------------------------------------

    async def register_worker(self, worker_id: str, ws: WebSocket) -> bool:
        r"""Register *ws* as the active worker for *worker_id*.

        Clears any stale hijack state from a previous crashed worker session.
        Returns \`\`True\`\` if a previous hijack was active (caller should broadcast
        a cleared-hijack notification), \`\`False\`\` otherwise.
        """
        with tracer.start_as_current_span("uterm.worker.register", attributes={"worker_id": worker_id}):
            async with self._lock:
                st = self._workers.setdefault(worker_id, WorkerTermState())
                st.events = deque(st.events, maxlen=self._event_deque_maxlen)
                # Only clear hijack state when the EXISTING lease is
                # actually expired. Worker WS reconnects are routine for
                # passive supervised bots (Cloudflare DO rotation, manager
                # restart, network blip) and the framework's hijack lease
                # should survive a transient reconnect — clearing it
                # unconditionally meant a single CFDO "reconnecting..."
                # blip mid-run silently invalidated the holder's
                # hijack_id, every subsequent /send 404'd, and the
                # whole compare run cratered. Time-bounded expiry
                # (lease_expires_at) is already the security guarantee;
                # WS register is not a security event.
                _now_mono = time.monotonic()
                _expired = st.hijack_session is not None and st.hijack_session.lease_expires_at <= _now_mono
                prev_was_hijacked = _expired or (st.hijack_session is None and st.hijack_owner is not None)
                if _expired:
                    st.hijack_session = None
                if prev_was_hijacked:
                    st.hijack_owner = None
                    st.hijack_owner_expires_at = None
                st.worker_ws = ws
            logger.info(EVENT_SESSION_REGISTERED, worker_id=worker_id, session_type="worker")
            return prev_was_hijacked

    async def is_active_worker(self, worker_id: str, ws: WebSocket) -> bool:
        """Return True if *ws* is still the registered worker for *worker_id*."""
        async with self._lock:
            st = self._workers.get(worker_id)
            return st is not None and st.worker_ws is ws

    async def set_worker_tunnel_flag(self, worker_id: str, value: bool) -> None:
        """Mark whether ``worker_id``'s worker WS uses the tunnel wire format.

        See :class:`WorkerTermState.is_tunnel_worker` for the semantics
        (raw bytes for input, no DLE-framed JSON envelope). Called by
        :mod:`provide.uterm.tunnel.fastapi_routes` right after
        ``register_worker`` so :meth:`send_worker` can route outbound
        messages with the correct codec.
        """
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is not None:
                st.is_tunnel_worker = value

    async def set_worker_hello(self, worker_id: str, mode: InputMode, protocol_version: int | None = None) -> bool:
        r"""Process a \`\`worker_hello\`\` message: set input_mode and persist protocol version.

        Returns \`\`True\`\` if the mode was applied, \`\`False\`\` if the worker is no
        longer registered or if switching to \`\`"open"\`\` while a hijack lease is
        active (mode change is blocked in that case). When ``protocol_version`` is
        provided, it is recorded on the :class:`WorkerTermState` so downstream
        feature gates can query it via ``worker.protocol_version``.
        """
        if protocol_version is not None:
            logger.info("worker_hello_protocol worker_id=%s version=%d", worker_id, protocol_version)
            if protocol_version < 1:
                logger.warning("worker_hello_legacy_protocol worker_id=%s version=%d", worker_id, protocol_version)

        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return False
            if mode == "open" and self.is_hijacked(st):
                logger.warning(
                    "worker_hello_mode_blocked worker_id=%s — cannot switch to open while hijack active",
                    worker_id,
                )
                return False
            st.input_mode = mode
            if protocol_version is not None:
                st.protocol_version = protocol_version
        return True

    async def update_last_snapshot(self, worker_id: str, snapshot: dict[str, Any]) -> None:
        """Store *snapshot* as the most recent snapshot for *worker_id*."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is not None:  # pragma: no branch
                st.last_snapshot = snapshot

    async def deregister_worker(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        r"""Clear *ws* as the active worker if it is still current.

        Returns \`\`(should_broadcast_disconnect, was_hijacked)\`\`.
        \`\`should_broadcast_disconnect\`\` is \`\`True\`\` only when *ws* was the
        current worker (i.e. a replacement has not already taken over).
        """
        with tracer.start_as_current_span("uterm.worker.deregister", attributes={"worker_id": worker_id}):
            async with self._lock:
                st = self._workers.get(worker_id)
                if st is None or st.worker_ws is not ws:
                    return False, False
                was_hijacked = st.hijack_session is not None or st.hijack_owner is not None
                st.worker_ws = None
                st.hijack_session = None
                st.hijack_owner = None
                st.hijack_owner_expires_at = None
            return True, was_hijacked

    # -- Browser connection lifecycle ------------------------------------------

    async def register_browser(
        self, worker_id: str, ws: WebSocket, role: str, *, defer_broadcast: bool = False
    ) -> dict[str, Any]:
        r"""Register *ws* as a browser for *worker_id* and return initial state.

        Returns a dict with keys: \`\`is_hijacked\`\`, \`\`hijacked_by_me\`\`,
        \`\`worker_online\`\`, \`\`input_mode\`\`, \`\`initial_snapshot\`\`,
        and optionally \`\`resume_token\`\`.
        """
        with tracer.start_as_current_span("uterm.browser.register", attributes={"worker_id": worker_id, "role": role}):
            resume_token: str | None = None
            if self._resume_store is not None:
                resume_token = await self._resume_store.create(worker_id, role, self._resume_ttl_s)
                self._ws_to_resume_token[ws] = resume_token
            async with self._lock:
                st = self._workers.setdefault(worker_id, WorkerTermState())
                st.browsers[ws] = role
                if defer_broadcast:
                    self._startup_pending_browsers.add(ws)
                initial_state = {
                    "is_hijacked": self.is_hijacked(st),
                    "hijacked_by_me": self.is_dashboard_hijack_active(st) and st.hijack_owner is ws,
                    "worker_online": st.worker_ws is not None,
                    "input_mode": st.input_mode,
                    "initial_snapshot": st.last_snapshot,
                    "resume_token": resume_token,
                }
            logger.info(EVENT_SESSION_REGISTERED, worker_id=worker_id, session_type="browser", role=role)
            return initial_state

    async def activate_browser_broadcasts(self, worker_id: str, ws: WebSocket) -> None:
        """Allow broadcasts to a browser after its startup frames have been sent."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if (
                st is not None and ws in st.browsers
            ):  # pragma: no branch — race window during browser disconnect; defensive
                self._startup_pending_browsers.discard(ws)

    @staticmethod
    def _scan_events_for_resume(st: Any) -> bool:
        r"""Scan event history to determine if a resume is still needed on browser disconnect.

        Returns \`\`True\`\` if a resume control frame should be sent (no prior expiry
        or release event was found in the history that would have already sent one).
        Scans backwards; stops at the first hijack lifecycle event encountered.
        """
        for evt in reversed(st.events):
            t = str(evt.get("type", ""))
            if t in {"hijack_owner_expired", "hijack_lease_expired"}:
                return False
            if t in {"hijack_acquired", "hijack_released"}:
                break
        return True

    def _update_lock_state(self, st: Any, ws: Any, owned_hijack: bool) -> tuple[bool, bool, bool]:
        r"""Apply disconnect state mutations to *st* and return outcome flags.

        Returns \`\`(was_owner, rest_still_active, resume_without_owner)\`\`.
        Must be called while holding \`\`self._lock\`\`.
        """
        was_owner = self.is_dashboard_hijack_active(st) and st.hijack_owner is ws
        rest_still_active = False
        resume_without_owner = False
        st.browsers.pop(ws, None)
        if was_owner:
            st.hijack_owner = None
            st.hijack_owner_expires_at = None
            rest_still_active = self.has_valid_rest_lease(st)
        elif owned_hijack and st.worker_ws is not None and not self.is_hijacked(st):  # pragma: no branch
            # Scan backwards for the most recent hijack-related event to determine
            # whether cleanup already sent a resume (lease/owner expired) or whether
            # a resume is still needed.  Checking only the last event is fragile
            # because a subsequent snapshot event can overwrite the expiry marker.
            resume_without_owner = self._scan_events_for_resume(st)
        return was_owner, rest_still_active, resume_without_owner

    async def cleanup_browser_disconnect(self, worker_id: str, ws: WebSocket, owned_hijack: bool) -> dict[str, Any]:
        r"""Handle a browser WS disconnect atomically.

        Returns a dict with keys: \`\`was_owner\`\`, \`\`resume_without_owner\`\`,
        \`\`rest_still_active\`\`.
        """
        with tracer.start_as_current_span("uterm.browser.deregister", attributes={"worker_id": worker_id}):
            browser_count = -1
            async with self._lock:
                st = self._workers.get(worker_id)
                was_owner = False
                rest_still_active = False
                resume_without_owner = False
                if st is not None:  # pragma: no branch
                    was_owner, rest_still_active, resume_without_owner = self._update_lock_state(st, ws, owned_hijack)
                    browser_count = len(st.browsers)
            # Mark resume token with hijack ownership (if any) so a reconnecting
            # browser can reclaim the lease.  Do NOT revoke — the token must survive
            # until the browser reconnects or TTL expires.
            if self._resume_store is not None:
                token = self._ws_to_resume_token.pop(ws, None)
                if token and (was_owner or owned_hijack):
                    await self._resume_store.mark_hijack_owner(token, True)
            self._startup_pending_browsers.discard(ws)

            # Fire empty-browser callback outside the lock when the last browser left.
            on_empty = getattr(self, "on_worker_empty", None)
            if browser_count == 0 and on_empty is not None:
                task = asyncio.create_task(on_empty(worker_id))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            logger.info(EVENT_SESSION_DISCONNECTED, worker_id=worker_id, session_type="browser")
            return {
                "was_owner": was_owner,
                "rest_still_active": rest_still_active,
                "resume_without_owner": resume_without_owner,
            }

    async def register_browser_state_snapshot(self, worker_id: str, ws: WebSocket) -> dict[str, Any]:
        """Return current browser state without re-registering.

        Used after a resume to get updated hello fields.
        """
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return {
                    "is_hijacked": False,
                    "hijacked_by_me": False,
                    "worker_online": False,
                    "input_mode": "hijack",
                }
            return {
                "is_hijacked": self.is_hijacked(st),
                "hijacked_by_me": self.is_dashboard_hijack_active(st) and st.hijack_owner is ws,
                "worker_online": st.worker_ws is not None,
                "input_mode": st.input_mode,
            }

    async def resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str:
        r"""Public wrapper around \`\`_resolve_role_for_browser\`\`."""
        return await self._resolve_role_for_browser(ws, worker_id)

    # -- Misc connection helpers -----------------------------------------------

    def can_send_input(self, st: WorkerTermState, ws: WebSocket) -> bool:
        """Check if *ws* can send input to the worker (open mode or hijack owner).

        In open mode, viewers are excluded — only operators and admins may send.
        """
        if st.input_mode == "open":
            role = st.browsers.get(ws, "viewer")
            return role in ("operator", "admin")
        return self.is_dashboard_hijack_active(st) and st.hijack_owner is ws

    async def request_snapshot(self, worker_id: str) -> None:
        """Send a snapshot_req control frame to the worker (no-op if no worker connected)."""
        await self.send_worker(worker_id, {"type": "snapshot_req", "req_id": str(uuid.uuid4()), "ts": time.time()})

    async def request_analysis(self, worker_id: str) -> None:
        """Send an analyze_req control frame to the worker (no-op if no worker connected)."""
        await self.send_worker(worker_id, {"type": "analyze_req", "req_id": str(uuid.uuid4()), "ts": time.time()})

    async def force_release_hijack(self, worker_id: str) -> bool:
        r"""Forcibly clear any active hijack for *worker_id* and send a resume control frame.

        Returns \`\`True\`\` if a hijack was active and was cleared, \`\`False\`\` otherwise.
        Typically called before switching input mode to \`\`"open"\`\` or on session teardown.
        """
        owner = "server-forced"
        had_hijack = False
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return False
            if st.hijack_session is not None:
                owner = st.hijack_session.owner
                st.hijack_session = None
                had_hijack = True
            if self.is_dashboard_hijack_active(st):  # pragma: no branch
                st.hijack_owner = None
                st.hijack_owner_expires_at = None
                had_hijack = True
        if not had_hijack:
            return False
        await self.send_worker(
            worker_id,
            {"type": "control", "action": "resume", "owner": owner, "lease_s": 0, "ts": time.time()},
        )
        self.notify_hijack_changed(worker_id, enabled=False, owner=None)
        await self.broadcast_hijack_state(worker_id)
        return True
