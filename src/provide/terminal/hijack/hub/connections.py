#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Connection lifecycle mixin for TermHub.

Extracted from ``core.py`` to keep file sizes under 500 LOC.
Provides public methods used by WS route handlers to register and
deregister workers/browsers without accessing hub internals directly.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger

from provide.terminal.hijack.models import WorkerTermState
from provide.terminal.hijack.ratelimit import TokenBucket

logger = get_logger(__name__)

# Keeps strong references to fire-and-forget tasks so CPython's GC cannot
# collect them before the event loop runs them.  Each task removes itself
# on completion via the done callback.
_background_tasks: set[asyncio.Task[Any]] = set()

# Maximum number of per-client rate-limit buckets held in memory at once.
# On overflow the oldest half of entries are evicted (LRU-lite), preserving
# rate-limit state for recently-active clients while bounding memory growth.
_REST_CLIENT_CACHE_MAX = 1024
_REST_CLIENT_EVICT_COUNT = _REST_CLIENT_CACHE_MAX // 2

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import WebSocket


class _ConnectionMixin:
    """Mixin providing worker/browser connection lifecycle methods for TermHub.

    Requires the host class to provide: ``_lock``, ``_workers``,
    ``_worker_token``, ``is_hijacked``, ``is_dashboard_hijack_active``,
    ``has_valid_rest_lease``, ``send_worker``, ``broadcast_hijack_state``,
    ``notify_hijack_changed``, ``_resolve_role_for_browser``.
    """

    # -- Declared attributes required from the host class ----------------------
    _lock: asyncio.Lock
    _workers: dict[str, WorkerTermState]
    _worker_token: str | None
    _event_deque_maxlen: int
    _rest_acquire_rate: float
    _rest_send_rate: float
    _rest_acquire_bucket: TokenBucket
    _rest_send_bucket: TokenBucket
    _rest_acquire_per_client: dict[str, TokenBucket]
    _rest_send_per_client: dict[str, TokenBucket]

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
        """Per-client REST acquire rate limit (also checks the global bucket).

        The per-client dict is capped at ``_REST_CLIENT_CACHE_MAX`` entries.
        On overflow the oldest half of entries are evicted so recently-active
        clients keep their rate-limit state (avoids a full-clear DoS vector).
        """
        if len(self._rest_acquire_per_client) >= _REST_CLIENT_CACHE_MAX:
            for k in list(self._rest_acquire_per_client)[:_REST_CLIENT_EVICT_COUNT]:
                del self._rest_acquire_per_client[k]
        bucket = self._rest_acquire_per_client.setdefault(client_id, TokenBucket(self._rest_acquire_rate))
        return bucket.allow() and self._rest_acquire_bucket.allow()

    def allow_rest_send_for(self, client_id: str) -> bool:
        """Per-client REST send/step rate limit (also checks the global bucket).

        On overflow the oldest half of entries are evicted (same LRU-lite
        strategy as :meth:`allow_rest_acquire_for`).
        """
        if len(self._rest_send_per_client) >= _REST_CLIENT_CACHE_MAX:
            for k in list(self._rest_send_per_client)[:_REST_CLIENT_EVICT_COUNT]:
                del self._rest_send_per_client[k]
        bucket = self._rest_send_per_client.setdefault(client_id, TokenBucket(self._rest_send_rate))
        return bucket.allow() and self._rest_send_bucket.allow()

    # -- Token access ----------------------------------------------------------

    def worker_token(self) -> str | None:
        """Return the configured worker bearer token (read-only)."""
        return self._worker_token

    # -- Worker connection lifecycle --------------------------------------------

    async def register_worker(self, worker_id: str, ws: WebSocket) -> bool:
        """Register *ws* as the active worker for *worker_id*.

        Clears any stale hijack state from a previous crashed worker session.
        Returns ``True`` if a previous hijack was active (caller should broadcast
        a cleared-hijack notification), ``False`` otherwise.
        """
        async with self._lock:
            st = self._workers.setdefault(worker_id, WorkerTermState())
            st.events = deque(st.events, maxlen=self._event_deque_maxlen)
            prev_was_hijacked = st.hijack_session is not None or st.hijack_owner is not None
            if prev_was_hijacked:
                st.hijack_session = None
                st.hijack_owner = None
                st.hijack_owner_expires_at = None
            st.worker_ws = ws
        return prev_was_hijacked

    async def is_active_worker(self, worker_id: str, ws: WebSocket) -> bool:
        """Return True if *ws* is still the registered worker for *worker_id*."""
        async with self._lock:
            st = self._workers.get(worker_id)
            return st is not None and st.worker_ws is ws

    async def set_worker_hello_mode(self, worker_id: str, mode: str) -> bool:
        """Set input_mode from a ``worker_hello`` message.

        Returns ``True`` if the mode was applied, ``False`` if the worker is no
        longer registered or if switching to ``"open"`` while a hijack lease is
        active (mode change is blocked in that case).
        """
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
        return True

    async def update_last_snapshot(self, worker_id: str, snapshot: dict[str, Any]) -> None:
        """Store *snapshot* as the most recent snapshot for *worker_id*."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is not None:  # pragma: no branch
                st.last_snapshot = snapshot

    async def deregister_worker(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        """Clear *ws* as the active worker if it is still current.

        Returns ``(should_broadcast_disconnect, was_hijacked)``.
        ``should_broadcast_disconnect`` is ``True`` only when *ws* was the
        current worker (i.e. a replacement has not already taken over).
        """
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

    async def register_browser(self, worker_id: str, ws: WebSocket, role: str) -> dict[str, Any]:
        """Register *ws* as a browser for *worker_id* and return initial state.

        Returns a dict with keys: ``is_hijacked``, ``hijacked_by_me``,
        ``worker_online``, ``input_mode``, ``initial_snapshot``.
        """
        async with self._lock:
            st = self._workers.setdefault(worker_id, WorkerTermState())
            st.browsers[ws] = role
            return {
                "is_hijacked": self.is_hijacked(st),
                "hijacked_by_me": self.is_dashboard_hijack_active(st) and st.hijack_owner is ws,
                "worker_online": st.worker_ws is not None,
                "input_mode": st.input_mode,
                "initial_snapshot": st.last_snapshot,
            }

    async def cleanup_browser_disconnect(self, worker_id: str, ws: WebSocket, owned_hijack: bool) -> dict[str, Any]:
        """Handle a browser WS disconnect atomically.

        Returns a dict with keys: ``was_owner``, ``resume_without_owner``,
        ``rest_still_active``.
        """
        browser_count = -1
        async with self._lock:
            st = self._workers.get(worker_id)
            was_owner = st is not None and self.is_dashboard_hijack_active(st) and st.hijack_owner is ws
            rest_still_active = False
            resume_without_owner = False
            if st is not None:  # pragma: no branch
                st.browsers.pop(ws, None)
                browser_count = len(st.browsers)
                if was_owner:
                    st.hijack_owner = None
                    st.hijack_owner_expires_at = None
                    rest_still_active = self.has_valid_rest_lease(st)
                elif owned_hijack and st.worker_ws is not None and not self.is_hijacked(st):  # pragma: no branch
                    # Scan backwards for the most recent hijack-related event to determine
                    # whether cleanup already sent a resume (lease/owner expired) or whether
                    # a resume is still needed.  Checking only the last event is fragile
                    # because a subsequent snapshot event can overwrite the expiry marker.
                    resume_without_owner = True
                    for evt in reversed(st.events):
                        t = str(evt.get("type", ""))
                        if t in {"hijack_owner_expired", "hijack_lease_expired"}:
                            resume_without_owner = False
                            break
                        if t in {"hijack_acquired", "hijack_released"}:
                            break
        # Fire empty-browser callback outside the lock when the last browser left.
        on_empty = getattr(self, "on_worker_empty", None)
        if browser_count == 0 and on_empty is not None:
            task = asyncio.create_task(on_empty(worker_id))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        return {
            "was_owner": was_owner,
            "rest_still_active": rest_still_active,
            "resume_without_owner": resume_without_owner,
        }

    async def resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str:
        """Public wrapper around ``_resolve_role_for_browser``."""
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
        """Forcibly clear any active hijack for *worker_id* and send a resume control frame.

        Returns ``True`` if a hijack was active and was cleared, ``False`` otherwise.
        Typically called before switching input mode to ``"open"`` or on session teardown.
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
