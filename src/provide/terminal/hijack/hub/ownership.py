#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Hijack ownership and lease management mixin for TermHub.

Extracted from ``hub.py`` to keep file sizes under 500 LOC.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from provide.terminal.hijack.models import HijackSession

if TYPE_CHECKING:
    import asyncio

    from fastapi import WebSocket

    from provide.terminal.hijack.models import WorkerTermState

logger = logging.getLogger(__name__)


class _HijackOwnershipMixin:
    """Mixin providing hijack ownership/lease methods for TermHub.

    Requires the host class to provide: ``_lock``, ``_workers``,
    ``_dashboard_hijack_lease_s``, ``_is_hijacked``, ``_is_dashboard_hijack_active``,
    ``_is_rest_session_active``, ``_send_worker``, ``_broadcast_hijack_state``,
    ``_append_event``, ``_prune_if_idle``, ``_notify_hijack_changed``.
    """

    # -- Typed self helpers (avoid repeating the cast everywhere) ---------------
    _lock: asyncio.Lock
    _workers: dict[str, WorkerTermState]
    _dashboard_hijack_lease_s: int

    async def _cleanup_expired_hijack(self, worker_id: str) -> bool:
        now = time.time()
        rest_expired = False
        dashboard_expired = False
        should_resume = False

        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return False
            # Fast path: nothing to expire — avoids the time comparisons below.
            if st.hijack_session is None and st.hijack_owner is None:
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

        if rest_expired or dashboard_expired:
            self._metric("hijack_lease_expiries_total")  # type: ignore[attr-defined]

        if should_resume:
            # Re-check under lock: a concurrent hijack_acquire may have written a
            # new session between the first lock release and _send_worker.
            async with self._lock:
                st2 = self._workers.get(worker_id)
                if st2 is not None and self._is_hijacked(st2):  # type: ignore[attr-defined]
                    should_resume = False
        if should_resume:
            await self._send_worker(  # type: ignore[attr-defined]
                worker_id,
                {"type": "control", "action": "resume", "owner": "lease-expired", "lease_s": 0, "ts": now},
            )
            self._notify_hijack_changed(worker_id, enabled=False, owner=None)  # type: ignore[attr-defined]

        if rest_expired:
            await self._append_event(worker_id, "hijack_lease_expired")  # type: ignore[attr-defined]
        if dashboard_expired:
            await self._append_event(worker_id, "hijack_owner_expired")  # type: ignore[attr-defined]
        await self._broadcast_hijack_state(worker_id)  # type: ignore[attr-defined]
        await self._prune_if_idle(worker_id)  # type: ignore[attr-defined]
        return True

    async def _get_rest_session(self, worker_id: str, hijack_id: str) -> HijackSession | None:
        await self._cleanup_expired_hijack(worker_id)
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return None
            hs = st.hijack_session
            if hs is None or hs.lease_expires_at <= time.time() or hs.hijack_id != hijack_id:
                return None
            return hs

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

        Returns:
            ``(True, None)`` on success.
            ``(False, "no_worker")`` if the worker disconnected before the lock.
            ``(False, "already_hijacked")`` if another session is active.
        """
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or st.worker_ws is None:
                return False, "no_worker"
            if self._is_dashboard_hijack_active(st) or self._is_rest_session_active(st):  # type: ignore[attr-defined]
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
            if self._is_dashboard_hijack_active(st) or self._is_rest_session_active(st):  # type: ignore[attr-defined]
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
            if st is None or not self._is_dashboard_hijack_active(st) or st.hijack_owner is not ws:  # type: ignore[attr-defined]
                return None
            st.hijack_owner_expires_at = time.time() + self._dashboard_hijack_lease_s
            return st.hijack_owner_expires_at

    async def _try_release_ws_hijack(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        """Atomically verify ownership and clear it in a single lock block.

        Returns:
            ``(released, rest_active)`` where *released* is ``True`` if *ws*
            was the active dashboard hijack owner and was cleared.
        """
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or not self._is_dashboard_hijack_active(st) or st.hijack_owner is not ws:  # type: ignore[attr-defined]
                rest_active = st is not None and self._is_rest_session_active(st)  # type: ignore[attr-defined]
                return False, rest_active
            st.hijack_owner = None
            st.hijack_owner_expires_at = None
            rest_active = self._is_rest_session_active(st)  # type: ignore[attr-defined]
        return True, rest_active

    async def _remove_dead_browsers(self, worker_id: str, dead: set[WebSocket]) -> bool:
        """Remove *dead* browser sockets from worker state under lock.

        If the dashboard hijack owner was among the dead sockets, clears the
        lease and sends a resume control frame (unless a REST session is still
        active).

        Returns ``True`` if the hijack state changed (owner cleared and resumed).
        """
        notify_hijack_off = False
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is not None:
                for ws in dead:
                    st.browsers.pop(ws, None)
                    if self._is_dashboard_hijack_active(st) and st.hijack_owner is ws:  # type: ignore[attr-defined]
                        st.hijack_owner = None
                        st.hijack_owner_expires_at = None
                        notify_hijack_off = not self._is_rest_session_active(st)  # type: ignore[attr-defined]
        if notify_hijack_off:
            # Re-check: a concurrent acquire may have written a new session
            # between the lock release above and _send_worker below.
            async with self._lock:
                _st2 = self._workers.get(worker_id)
                if _st2 is not None and self._is_hijacked(_st2):  # type: ignore[attr-defined]
                    notify_hijack_off = False
        if notify_hijack_off:
            await self._send_worker(  # type: ignore[attr-defined]
                worker_id,
                {"type": "control", "action": "resume", "owner": "dead-socket", "lease_s": 0, "ts": time.time()},
            )
            self._notify_hijack_changed(worker_id, enabled=False, owner=None)  # type: ignore[attr-defined]
        return notify_hijack_off
