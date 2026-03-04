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
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

try:
    from fastapi import WebSocket
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for TermHub: pip install 'provide-terminal[websocket]'") from _e

import logging

from provide.terminal.hijack.models import WorkerTermState, HijackSession, extract_prompt_id

logger = logging.getLogger(__name__)

# Callback: (worker_id, is_hijacked, owner_or_None)
HijackStateCallback = Callable[[str, bool, str | None], Awaitable[None] | None]


class TermHub:
    """In-memory registry for terminal WebSocket connections.

    Manages the lifecycle of worker ↔ browser terminal streams and hijack leases.

    Args:
        on_hijack_changed: Optional async or sync callback invoked whenever hijack
            state changes for any worker. Signature: ``(worker_id, hijacked, owner) -> None``.
        dashboard_hijack_lease_s: Default dashboard WS hijack lease duration in seconds.
    """

    def __init__(
        self,
        on_hijack_changed: HijackStateCallback | None = None,
        dashboard_hijack_lease_s: int = 45,
    ) -> None:
        self._lock = asyncio.Lock()
        self._workers: dict[str, WorkerTermState] = {}
        self._on_hijack_changed = on_hijack_changed
        self._dashboard_hijack_lease_s = max(1, min(int(dashboard_hijack_lease_s), 600))

    @staticmethod
    def _clamp_lease(lease_s: int) -> int:
        return max(1, min(int(lease_s), 3600))

    @staticmethod
    def _is_rest_session_active(st: WorkerTermState) -> bool:
        hs = st.hijack_session
        return hs is not None and hs.lease_expires_at > time.time()

    @staticmethod
    def _is_dashboard_hijack_active(st: WorkerTermState) -> bool:
        if st.hijack_owner is None:
            return False
        if st.hijack_owner_expires_at is None:
            return True
        return st.hijack_owner_expires_at > time.time()

    def _is_hijacked(self, st: WorkerTermState) -> bool:
        return self._is_dashboard_hijack_active(st) or self._is_rest_session_active(st)

    async def _get(self, worker_id: str) -> WorkerTermState:
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                st = WorkerTermState()
                self._workers[worker_id] = st
            return st

    def _notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        cb = self._on_hijack_changed
        if cb is None:
            return
        result = cb(worker_id, enabled, owner)
        if inspect.isawaitable(result):
            task = asyncio.create_task(result)  # type: ignore[arg-type]
            task.add_done_callback(
                lambda t: (
                    logger.warning("on_hijack_changed callback raised worker_id=%s error=%s", worker_id, t.exception())
                    if not t.cancelled() and t.exception() is not None
                    else None
                )
            )

    async def _append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = data or {}
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                # Worker was pruned before this event could be recorded; drop it
                # rather than resurrecting a ghost WorkerTermState.
                return {"seq": 0, "ts": time.time(), "type": event_type, "data": payload}
            st.event_seq += 1
            evt: dict[str, Any] = {"seq": st.event_seq, "ts": time.time(), "type": event_type, "data": payload}
            st.events.append(evt)
            return evt

    async def _cleanup_expired_hijack(self, worker_id: str) -> bool:
        now = time.time()
        rest_expired = False
        dashboard_expired = False
        should_resume = False

        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return False

            if st.hijack_session is not None and st.hijack_session.lease_expires_at <= now:
                st.hijack_session = None
                rest_expired = True

            if (
                st.hijack_owner is not None
                and st.hijack_owner_expires_at is not None
                and st.hijack_owner_expires_at <= now
            ):
                st.hijack_owner = None
                st.hijack_owner_expires_at = None
                dashboard_expired = True

            should_resume = (
                (rest_expired or dashboard_expired) and st.hijack_owner is None and st.hijack_session is None
            )

        if not rest_expired and not dashboard_expired:
            return False

        if should_resume:
            await self._send_worker(
                worker_id,
                {"type": "control", "action": "resume", "owner": "lease-expired", "lease_s": 0, "ts": now},
            )
            self._notify_hijack_changed(worker_id, enabled=False, owner=None)

        if rest_expired:
            await self._append_event(worker_id, "hijack_lease_expired")
        if dashboard_expired:
            await self._append_event(worker_id, "hijack_owner_expired")
        await self._broadcast_hijack_state(worker_id)
        await self._prune_if_idle(worker_id)
        return True

    async def _get_rest_session(self, worker_id: str, hijack_id: str) -> HijackSession | None:
        await self._cleanup_expired_hijack(worker_id)
        # Re-read hijack_session under the lock to avoid a TOCTOU window where
        # _cleanup_expired_hijack drops the lock and a concurrent request clears
        # or replaces the session before we inspect it.
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return None
            hs = st.hijack_session
            if hs is None or hs.lease_expires_at <= time.time() or hs.hijack_id != hijack_id:
                return None
            return hs

    @staticmethod
    def _snapshot_matches(
        snapshot: dict[str, Any] | None,
        *,
        expect_prompt_id: str | None,
        expect_regex: re.Pattern[str] | None,
    ) -> bool:
        if snapshot is None:
            return False
        if expect_prompt_id and extract_prompt_id(snapshot) != expect_prompt_id:
            return False
        return not (expect_regex is not None and not expect_regex.search(str(snapshot.get("screen", ""))))

    async def _wait_for_snapshot(self, worker_id: str, timeout_ms: int = 1500) -> dict[str, Any] | None:
        req_ts = time.time()
        end = req_ts + timeout_ms / 1000.0
        await self._request_snapshot(worker_id)
        while time.time() < end:
            async with self._lock:
                st = self._workers.get(worker_id)
                if st is None:
                    return None
                snap = st.last_snapshot
            if snap is not None and snap.get("ts", 0) > req_ts:
                return snap
            await asyncio.sleep(0.08)
        async with self._lock:
            st = self._workers.get(worker_id)
            return st.last_snapshot if st is not None else None

    async def _wait_for_guard(
        self,
        worker_id: str,
        *,
        expect_prompt_id: str | None,
        expect_regex: str | None,
        timeout_ms: int,
        poll_interval_ms: int,
    ) -> tuple[bool, dict[str, Any] | None, str | None]:
        regex_obj: re.Pattern[str] | None = None
        if expect_regex:
            try:
                regex_obj = re.compile(expect_regex, re.IGNORECASE | re.MULTILINE)
            except re.error as exc:
                return False, None, f"invalid expect_regex: {exc}"

        if not expect_prompt_id and regex_obj is None:
            # No guard constraints: return the most recently cached snapshot.
            # Contract: callers receive whatever was last broadcast by the worker;
            # the value may be stale if the worker has been idle.  A snapshot_req
            # is fired so the next caller gets a fresher value, but this call does
            # not wait for the worker's response.
            async with self._lock:
                st = self._workers.get(worker_id)
                snap = st.last_snapshot if st is not None else None
            await self._request_snapshot(worker_id)
            return True, snap, None

        end = time.time() + max(50, timeout_ms) / 1000.0
        interval = max(20, poll_interval_ms) / 1000.0
        last_snapshot: dict[str, Any] | None = None
        while time.time() < end:
            async with self._lock:
                st = self._workers.get(worker_id)
                last_snapshot = st.last_snapshot if st is not None else None
            if self._snapshot_matches(last_snapshot, expect_prompt_id=expect_prompt_id, expect_regex=regex_obj):
                return True, last_snapshot, None
            await self._request_snapshot(worker_id)
            await asyncio.sleep(interval)

        return False, last_snapshot, "prompt_guard_not_satisfied"

    async def _broadcast(self, worker_id: str, msg: dict[str, Any]) -> None:
        # Snapshot browsers under the lock — mirrors _broadcast_hijack_state.
        # A concurrent disconnect finally block can mutate st.browsers between
        # the _get() lock release and iteration, so we must hold the lock while
        # taking the snapshot.
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return
            browsers = list(st.browsers)
        dead: set[WebSocket] = set()
        payload = json.dumps(msg, ensure_ascii=True)
        for ws in browsers:
            try:
                await ws.send_text(payload)
            except Exception as exc:
                logger.debug("broadcast_send_failed worker_id=%s: %s", worker_id, exc)
                dead.add(ws)
        if dead:
            async with self._lock:
                st2 = self._workers.get(worker_id)
                if st2 is not None:
                    for ws in dead:
                        st2.browsers.discard(ws)

    async def _broadcast_hijack_state(self, worker_id: str) -> None:
        # Snapshot all mutable fields under the lock so that concurrent hijack
        # state changes during the async broadcast loop don't produce inconsistent
        # per-client messages (e.g. owner changing between two send_text awaits).
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return
            browsers = list(st.browsers)
            hijack_owner = st.hijack_owner
            is_hijacked = self._is_hijacked(st)
            is_dashboard = self._is_dashboard_hijack_active(st)
            is_rest = self._is_rest_session_active(st)
            lease_expires_at = (
                st.hijack_session.lease_expires_at
                if is_rest and st.hijack_session is not None
                else st.hijack_owner_expires_at
            )

        dead: set[WebSocket] = set()
        for ws in browsers:
            try:
                if is_dashboard and hijack_owner is ws:
                    owner: str | None = "me"
                elif is_dashboard or is_rest:
                    owner = "other"
                else:
                    owner = None
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "hijack_state",
                            "hijacked": is_hijacked,
                            "owner": owner,
                            "lease_expires_at": lease_expires_at,
                        },
                        ensure_ascii=True,
                    )
                )
            except Exception as exc:
                logger.debug("broadcast_hijack_state_send_failed worker_id=%s: %s", worker_id, exc)
                dead.add(ws)
        if dead:
            async with self._lock:
                st2 = self._workers.get(worker_id)
                if st2 is not None:
                    for ws in dead:
                        st2.browsers.discard(ws)

    async def _send_worker(self, worker_id: str, msg: dict[str, Any]) -> bool:
        # Capture ws under the lock: avoids both creating blank state for unknown
        # workers (the old _get used setdefault) and a TOCTOU window where a
        # concurrent disconnect could set worker_ws=None between lock release and
        # the send_text call below.
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or st.worker_ws is None:
                return False
            ws = st.worker_ws
        try:
            await ws.send_text(json.dumps(msg, ensure_ascii=True))
            return True
        except Exception as exc:
            logger.debug("send_worker_failed worker_id=%s: %s", worker_id, exc)
            async with self._lock:
                st2 = self._workers.get(worker_id)
                if st2 is not None and st2.worker_ws is ws:
                    st2.worker_ws = None
            return False

    async def _prune_if_idle(self, worker_id: str) -> None:
        """Remove worker state when no connections or leases remain."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return
            if (
                st.worker_ws is None
                and not st.browsers
                and st.hijack_owner is None
                and st.hijack_session is None
            ):
                del self._workers[worker_id]
                logger.debug("pruned idle worker_id=%s", worker_id)

    async def _try_acquire_rest_hijack(
        self,
        worker_id: str,
        *,
        owner: str,
        lease_s: int,
        hijack_id: str,
        now: float,
    ) -> tuple[bool, str | None]:
        """Atomically check availability and create a REST hijack session.

        Must be called *after* confirming the worker is present via
        :meth:`_send_worker`; this method re-validates liveness inside the
        lock so that a worker disconnect racing between :meth:`_send_worker`
        returning ``True`` and this method acquiring the lock cannot create
        an orphaned session (a ``HijackSession`` with no live ``worker_ws``
        that blocks future hijack attempts until the lease expires).

        Returns:
            ``(True, None)`` on success.
            ``(False, "no_worker")`` if the worker disconnected before the lock.
            ``(False, "already_hijacked")`` if another session is active.
        """
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or st.worker_ws is None:
                return False, "no_worker"
            if self._is_dashboard_hijack_active(st) or self._is_rest_session_active(st):
                return False, "already_hijacked"
            st.hijack_session = HijackSession(
                hijack_id=hijack_id,
                owner=owner,
                acquired_at=now,
                lease_expires_at=now + lease_s,
                last_heartbeat=now,
            )
        return True, None

    async def _try_acquire_ws_hijack(self, worker_id: str, ws: WebSocket) -> tuple[bool, str | None]:
        """Atomically check availability and set the dashboard WS hijack owner.

        Returns:
            ``(True, None)`` on success.
            ``(False, "no_worker")`` if no worker is connected.
            ``(False, "already_hijacked")`` if another hijack is active.
        """
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or st.worker_ws is None:
                return False, "no_worker"
            if self._is_dashboard_hijack_active(st) or self._is_rest_session_active(st):
                return False, "already_hijacked"
            ttl = self._dashboard_hijack_lease_s
            st.hijack_owner = ws
            st.hijack_owner_expires_at = time.time() + ttl
        return True, None

    async def _touch_hijack_owner(self, worker_id: str, lease_s: int | None = None) -> float | None:
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or st.hijack_owner is None:
                return None
            ttl = self._dashboard_hijack_lease_s if lease_s is None else max(1, min(int(lease_s), 600))
            st.hijack_owner_expires_at = time.time() + ttl
            return st.hijack_owner_expires_at

    async def _touch_if_owner(self, worker_id: str, ws: WebSocket) -> float | None:
        """Atomically verify WS ownership and extend lease; returns new expiry or None."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or not self._is_dashboard_hijack_active(st) or st.hijack_owner is not ws:
                return None
            st.hijack_owner_expires_at = time.time() + self._dashboard_hijack_lease_s
            return st.hijack_owner_expires_at

    async def _is_owner(self, worker_id: str, ws: WebSocket) -> bool:
        # Read under the lock so the owner identity check is not a TOCTOU with
        # concurrent hijack_request / hijack_release / disconnect handlers.
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return False
            return self._is_dashboard_hijack_active(st) and st.hijack_owner is ws

    async def _try_release_ws_hijack(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        """Atomically verify ownership and clear it in a single lock block.

        Prevents the TOCTOU window in voluntary ``hijack_release`` where
        a concurrent ``hijack_request`` could steal ownership between
        :meth:`_is_owner` returning ``True`` and :meth:`_set_hijack_owner`
        clearing the owner field.

        Also captures REST-session liveness inside the same lock block so that
        callers can decide whether to fire ``on_hijack_changed`` without a
        separate :meth:`_get` call outside the lock.

        Returns:
            ``(released, rest_active)`` where *released* is ``True`` if *ws*
            was the active dashboard hijack owner and was cleared, and
            *rest_active* is ``True`` if a REST hijack session is still active
            after the release.
        """
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or not self._is_dashboard_hijack_active(st) or st.hijack_owner is not ws:
                rest_active = st is not None and self._is_rest_session_active(st)
                return False, rest_active
            st.hijack_owner = None
            st.hijack_owner_expires_at = None
            rest_active = self._is_rest_session_active(st)
        return True, rest_active

    async def _hijack_state_msg_for(self, worker_id: str, ws: WebSocket) -> dict[str, Any]:
        # Snapshot all mutable fields under the lock — mirrors _broadcast_hijack_state
        # to prevent stale reads when concurrent hijack changes race against this call.
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return {"type": "hijack_state", "hijacked": False, "owner": None, "lease_expires_at": None}
            is_dashboard = self._is_dashboard_hijack_active(st)
            is_rest = self._is_rest_session_active(st)
            is_h = is_dashboard or is_rest
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
        return {
            "type": "hijack_state",
            "hijacked": is_h,
            "owner": owner,
            "lease_expires_at": lease_expires_at,
        }

    async def _request_snapshot(self, worker_id: str) -> None:
        await self._send_worker(worker_id, {"type": "snapshot_req", "req_id": str(uuid.uuid4()), "ts": time.time()})

    async def _request_analysis(self, worker_id: str) -> None:
        await self._send_worker(worker_id, {"type": "analyze_req", "req_id": str(uuid.uuid4()), "ts": time.time()})

    def create_router(self) -> Any:
        """Create and return a FastAPI ``APIRouter`` with all terminal routes registered."""
        from fastapi import APIRouter

        from provide.terminal.hijack.routes import register_rest_routes, register_ws_routes

        router = APIRouter()
        register_rest_routes(self, router)
        register_ws_routes(self, router)
        return router
