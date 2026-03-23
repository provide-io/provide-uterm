#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""TermHub: in-memory registry for terminal WebSocket connections.

Requires the ``websocket`` extra::

    pip install 'provide-terminal[websocket]'
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, cast

from provide.telemetry import get_logger

try:
    from fastapi import APIRouter, WebSocket, WebSocketException
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for TermHub: pip install 'provide-terminal[websocket]'") from _e

from provide.terminal.control_stream import encode_control, encode_data
from provide.terminal.hijack.frames import HijackStateFrame, make_hijack_state_frame, make_worker_disconnected_frame
from provide.terminal.hijack.hub.connections import _ConnectionMixin
from provide.terminal.hijack.hub.ownership import _HijackOwnershipMixin
from provide.terminal.hijack.hub.polling import _PollingMixin
from provide.terminal.hijack.hub.resume import ResumeSession, ResumeTokenStore
from provide.terminal.hijack.models import WorkerTermState
from provide.terminal.hijack.ratelimit import TokenBucket

logger = get_logger(__name__)
# Callback type aliases
HijackStateCallback = Callable[[str, bool, str | None], Awaitable[None] | None]
BrowserRoleResolver = Callable[[WebSocket, str], str | None | Awaitable[str | None]]
MetricCallback = Callable[[str, int], None]
WorkerEmptyCallback = Callable[[str], Coroutine[Any, Any, None]]
ResumeCallback = Callable[[str, ResumeSession], Awaitable[bool]]


def _encode_browser_frame(msg: dict[str, Any]) -> str:
    if str(msg.get("type") or "") == "term":
        return encode_data(str(msg.get("data") or ""))
    return encode_control(msg)


def _encode_worker_frame(msg: dict[str, Any]) -> str:
    if str(msg.get("type") or "") == "input":
        return encode_data(str(msg.get("data") or ""))
    return encode_control(msg)


class BrowserRoleResolutionError(RuntimeError):
    """Raised when a browser-role resolver fails and the WS should be rejected."""


class TermHub(_PollingMixin, _HijackOwnershipMixin, _ConnectionMixin):
    """In-memory registry for terminal WebSocket connections.

    Manages the lifecycle of worker / browser terminal streams and hijack leases.

    Args:
        on_hijack_changed: ``(worker_id, hijacked, owner) -> None`` fired on any
            hijack state change (async or sync).
        dashboard_hijack_lease_s: Default WS hijack lease duration in seconds.
        resolve_browser_role: ``(ws, worker_id) -> str | None`` — returns
            ``"viewer"``, ``"operator"``, or ``"admin"`` for each browser; ``None``
            defaults to ``"viewer"``. Raise :class:`BrowserRoleResolutionError`
            to close the socket with 1008.
    """

    def __init__(
        self,
        on_hijack_changed: HijackStateCallback | None = None,
        on_metric: MetricCallback | None = None,
        dashboard_hijack_lease_s: int = 45,
        *,
        resolve_browser_role: BrowserRoleResolver | None = None,
        on_worker_empty: WorkerEmptyCallback | None = None,
        max_ws_message_bytes: int = 1_048_576,
        max_input_chars: int = 10_000,
        browser_rate_limit_per_sec: float = 30,
        rest_acquire_rate_limit_per_sec: float = 5,
        rest_send_rate_limit_per_sec: float = 20,
        worker_token: str | None = None,
        event_deque_maxlen: int = 2000,
        resume_store: ResumeTokenStore | None = None,
        resume_ttl_s: float = 300,
        on_resume: ResumeCallback | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._workers: dict[str, WorkerTermState] = {}
        self._on_hijack_changed = on_hijack_changed
        self._on_metric = on_metric
        self._resolve_browser_role = resolve_browser_role
        self.on_worker_empty: WorkerEmptyCallback | None = on_worker_empty
        self._worker_token = worker_token
        self._dashboard_hijack_lease_s = max(1, min(int(dashboard_hijack_lease_s), 600))
        self.max_ws_message_bytes = max(1024, int(max_ws_message_bytes))
        self.max_input_chars = max(100, int(max_input_chars))
        self.browser_rate_limit_per_sec = float(browser_rate_limit_per_sec)
        self._rest_acquire_rate = max(0.1, float(rest_acquire_rate_limit_per_sec))
        self._rest_send_rate = max(0.1, float(rest_send_rate_limit_per_sec))
        self._rest_acquire_bucket = TokenBucket(self._rest_acquire_rate)
        self._rest_send_bucket = TokenBucket(self._rest_send_rate)
        # Per-client buckets; oldest half evicted when cache exceeds _REST_CLIENT_CACHE_MAX.
        self._rest_acquire_per_client: dict[str, TokenBucket] = {}
        self._rest_send_per_client: dict[str, TokenBucket] = {}
        self._event_deque_maxlen = max(1, int(event_deque_maxlen))
        self._resume_store = resume_store
        self._resume_ttl_s = max(1.0, float(resume_ttl_s))
        self._on_resume = on_resume
        self._ws_to_resume_token: dict[WebSocket, str] = {}

    def metric(self, name: str, value: int = 1) -> None:
        """Emit a named metric via the configured on_metric callback."""
        callback = self._on_metric
        if callback is None:
            return
        try:
            callback(name, int(value))
        except Exception as exc:  # pragma: no cover - defensive only
            logger.warning("metric_callback_failed metric=%s error=%s", name, exc)

    @staticmethod
    def clamp_lease(lease_s: int) -> int:
        """Clamp a lease duration to [1, 3600] seconds."""
        return max(1, min(int(lease_s), 3600))

    @staticmethod
    def has_valid_rest_lease(st: WorkerTermState) -> bool:
        """Return True if *st* has an unexpired REST hijack session."""
        hs = st.hijack_session
        return hs is not None and hs.lease_expires_at > time.time()

    @staticmethod
    def is_dashboard_hijack_active(st: WorkerTermState) -> bool:
        """Return True if a dashboard WS hijack owner exists and its lease has not expired."""
        if st.hijack_owner is None:
            return False
        if st.hijack_owner_expires_at is None:
            return True
        return st.hijack_owner_expires_at > time.time()

    def is_hijacked(self, st: WorkerTermState) -> bool:
        """Return True if *st* is under any active hijack (dashboard WS or REST)."""
        return self.is_dashboard_hijack_active(st) or self.has_valid_rest_lease(st)

    async def _get(self, worker_id: str) -> WorkerTermState:
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                st = WorkerTermState()
                self._workers[worker_id] = st
            return st

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        """Fire the on_hijack_changed callback (sync or async) without blocking."""
        cb = self._on_hijack_changed
        if cb is None:
            return
        result = cb(worker_id, enabled, owner)
        if inspect.isawaitable(result):
            task: asyncio.Task[object] = asyncio.create_task(result)  # type: ignore[arg-type]
            task.add_done_callback(
                lambda t: (
                    logger.warning("on_hijack_changed callback raised worker_id=%s error=%s", worker_id, t.exception())
                    if not t.cancelled() and t.exception() is not None
                    else None
                )
            )

    async def _resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str:
        role = "viewer"
        resolver = self._resolve_browser_role
        if resolver is None:
            return role
        try:
            resolved_role = resolver(ws, worker_id)
            if inspect.isawaitable(resolved_role):
                try:
                    resolved_role = await asyncio.wait_for(resolved_role, timeout=5.0)
                except TimeoutError as exc:
                    logger.warning("resolve_browser_role_timeout worker_id=%s", worker_id)
                    self.metric("browser_role_resolution_timeout")
                    raise BrowserRoleResolutionError(worker_id) from exc
        except (BrowserRoleResolutionError, WebSocketException):
            # Re-raise so the caller sees the original close code / error type.
            raise
        except Exception as exc:
            logger.warning("resolve_browser_role_failed worker_id=%s error=%s", worker_id, exc)
            raise BrowserRoleResolutionError(worker_id) from exc
        if isinstance(resolved_role, str) and resolved_role in {"viewer", "operator", "admin"}:
            return resolved_role
        if resolved_role is not None:
            logger.warning("resolve_browser_role_invalid worker_id=%s role=%r", worker_id, resolved_role)
        return role

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append a timestamped event to the worker's event ring buffer and return it."""
        payload = data or {}
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return {"seq": 0, "ts": time.time(), "type": event_type, "data": payload}
            st.event_seq += 1
            evt: dict[str, Any] = {"seq": st.event_seq, "ts": time.time(), "type": event_type, "data": payload}
            st.events.append(evt)
            # Set min_event_seq after append so it always reflects events[0].seq,
            # which is correct whether or not the deque was full before the append.
            st.min_event_seq = int(st.events[0]["seq"])
            return evt

    async def broadcast(self, worker_id: str, msg: dict[str, Any]) -> None:
        """Send *msg* to all browser WebSockets registered for *worker_id*."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return
            browsers = list(st.browsers.keys())
        dead: set[WebSocket] = set()
        payload = _encode_browser_frame(msg)
        for ws in browsers:
            try:
                await ws.send_text(payload)
            except Exception as exc:
                logger.debug("broadcast_send_failed worker_id=%s: %s", worker_id, exc)
                dead.add(ws)
        if dead:
            changed = await self.remove_dead_browsers(worker_id, dead)
            if changed:
                await self.broadcast_hijack_state(worker_id)

    async def _send_hijack_state_to(
        self,
        browsers: list[WebSocket],
        *,
        worker_id: str,
        is_hijacked: bool,
        is_dashboard: bool,
        is_rest: bool,
        hijack_owner: WebSocket | None,
        input_mode: str,
        lease_expires_at: float | None,
        suppress_errors: bool = False,
    ) -> set[WebSocket]:
        """Send a hijack_state message to each browser; return the set of dead sockets."""
        dead: set[WebSocket] = set()
        for ws in browsers:
            if is_dashboard and hijack_owner is ws:
                owner: str | None = "me"
            elif is_dashboard or is_rest:
                owner = "other"
            else:
                owner = None
            payload = encode_control(
                cast(
                    "dict[str, Any]",
                    make_hijack_state_frame(
                        hijacked=is_hijacked,
                        owner=owner,
                        lease_expires_at=lease_expires_at,
                        input_mode=input_mode,
                    ),
                )
            )
            try:
                await ws.send_text(payload)
            except Exception as exc:
                if not suppress_errors:
                    logger.debug("broadcast_hijack_state_send_failed worker_id=%s: %s", worker_id, exc)
                dead.add(ws)
        return dead

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        """Send a hijack_state message to every browser for *worker_id*, cleaning up dead sockets."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return
            browsers = list(st.browsers.keys())
            hijack_owner = st.hijack_owner
            is_hijacked = self.is_hijacked(st)
            is_dashboard = self.is_dashboard_hijack_active(st)
            is_rest = self.has_valid_rest_lease(st)
            input_mode = st.input_mode
            lease_expires_at = (
                st.hijack_session.lease_expires_at
                if is_rest and st.hijack_session is not None
                else st.hijack_owner_expires_at
            )

        dead = await self._send_hijack_state_to(
            browsers,
            worker_id=worker_id,
            is_hijacked=is_hijacked,
            is_dashboard=is_dashboard,
            is_rest=is_rest,
            hijack_owner=hijack_owner,
            input_mode=input_mode,
            lease_expires_at=lease_expires_at,
        )
        if dead:
            await self.remove_dead_browsers(worker_id, dead)
            # Re-read updated state and send to survivors directly — avoids recursion
            # when multiple browsers die simultaneously.
            async with self._lock:
                st2 = self._workers.get(worker_id)
                if st2 is None:
                    return
                survivors = list(st2.browsers.keys())
                is_h2 = self.is_hijacked(st2)
                is_dashboard2 = self.is_dashboard_hijack_active(st2)
                is_rest2 = self.has_valid_rest_lease(st2)
                hijack_owner2 = st2.hijack_owner
                input_mode2 = st2.input_mode
                lease2 = (
                    st2.hijack_session.lease_expires_at
                    if is_rest2 and st2.hijack_session is not None
                    else st2.hijack_owner_expires_at
                )
            await self._send_hijack_state_to(
                survivors,
                worker_id=worker_id,
                is_hijacked=is_h2,
                is_dashboard=is_dashboard2,
                is_rest=is_rest2,
                hijack_owner=hijack_owner2,
                input_mode=input_mode2,
                lease_expires_at=lease2,
                suppress_errors=True,
            )

    async def send_worker(self, worker_id: str, msg: dict[str, Any]) -> bool:
        """Send *msg* to the worker WebSocket; returns False if no worker is connected."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or st.worker_ws is None:
                return False
            ws = st.worker_ws
        try:
            await ws.send_text(_encode_worker_frame(msg))
            return True
        except Exception as exc:
            logger.debug("send_worker_failed worker_id=%s: %s", worker_id, exc)
            async with self._lock:
                st2 = self._workers.get(worker_id)
                if st2 is not None and st2.worker_ws is ws:  # pragma: no branch
                    st2.worker_ws = None
            return False

    async def prune_if_idle(self, worker_id: str) -> None:
        """Remove worker state when no connections or leases remain."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return
            if st.worker_ws is None and not st.browsers and st.hijack_owner is None and st.hijack_session is None:
                del self._workers[worker_id]
                logger.debug("pruned idle worker_id=%s", worker_id)

    async def hijack_state_msg_for(self, worker_id: str, ws: WebSocket) -> HijackStateFrame:
        """Build a hijack_state dict for *ws*, setting owner='me' if *ws* holds the lease."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return make_hijack_state_frame(
                    hijacked=False,
                    owner=None,
                    lease_expires_at=None,
                    input_mode="hijack",
                )
            is_dashboard = self.is_dashboard_hijack_active(st)
            is_rest = self.has_valid_rest_lease(st)
            is_h = is_dashboard or is_rest
            input_mode = st.input_mode
            lease_expires_at = (
                st.hijack_session.lease_expires_at
                if is_rest and st.hijack_session is not None
                else st.hijack_owner_expires_at
            )
            if is_dashboard and st.hijack_owner is ws:
                owner: str | None = "me"
            elif is_dashboard or is_rest:
                owner = "other"
            else:
                owner = None
        return make_hijack_state_frame(
            hijacked=is_h,
            owner=owner,
            lease_expires_at=lease_expires_at,
            input_mode=input_mode,
        )

    async def set_input_mode(self, worker_id: str, mode: str) -> tuple[bool, str | None]:
        """Set input_mode under lock. Rejects if active hijack when switching to "open".

        Returns:
            ``(True, None)`` on success.
            ``(False, "not_found")`` if worker not registered.
            ``(False, "active_hijack")`` if a hijack is active and mode is "open".
        """
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return False, "not_found"
            if mode == "open" and self.is_hijacked(st):
                return False, "active_hijack"
            st.input_mode = mode
        await self.broadcast(
            worker_id,
            {"type": "input_mode_changed", "input_mode": mode, "ts": time.time()},
        )
        await self.broadcast_hijack_state(worker_id)
        return True, None

    async def disconnect_worker(self, worker_id: str) -> bool:
        """Programmatically disconnect the worker WS. Returns True if a worker was connected."""
        ws: WebSocket | None = None
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or st.worker_ws is None:
                return False
            ws = st.worker_ws
            st.worker_ws = None
            # Clear hijack state atomically
            was_hijacked = st.hijack_session is not None or st.hijack_owner is not None
            st.hijack_session = None
            st.hijack_owner = None
            st.hijack_owner_expires_at = None
        # Close WS outside lock
        try:
            await ws.close()
        except Exception as exc:
            logger.debug("disconnect_worker close error worker_id=%s: %s", worker_id, exc)
        await self.broadcast(
            worker_id,
            cast("dict[str, Any]", make_worker_disconnected_frame(worker_id)),
        )
        if was_hijacked:
            self.notify_hijack_changed(worker_id, enabled=False, owner=None)
            await self.broadcast_hijack_state(worker_id)
        await self.prune_if_idle(worker_id)
        return True

    async def get_last_snapshot(self, worker_id: str) -> dict[str, Any] | None:
        """Return the most recent snapshot for *worker_id*, or ``None`` if not registered."""
        async with self._lock:
            st = self._workers.get(worker_id)
            return None if st is None else st.last_snapshot

    async def browser_count(self, worker_id: str) -> int:
        """Return the number of browser WebSockets currently connected for *worker_id*."""
        async with self._lock:
            st = self._workers.get(worker_id)
            return 0 if st is None else len(st.browsers)

    async def get_recent_events(self, worker_id: str, limit: int) -> list[dict[str, Any]]:
        """Return the most recent events for *worker_id* (up to *limit*, clamped to 1-500)."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return []
            return list(st.events)[-max(1, min(limit, 500)) :]

    def create_router(self) -> APIRouter:
        """Create and return a FastAPI ``APIRouter`` with all terminal routes registered."""
        from provide.terminal.hijack.routes.rest import register_rest_routes
        from provide.terminal.hijack.routes.websockets import register_ws_routes

        router = APIRouter()
        register_rest_routes(self, router)
        register_ws_routes(self, router)
        return router
