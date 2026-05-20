#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
r"""Hijack ownership and lease management mixin for TermHub.

Extracted from \`\`hub.py\`\` to keep file sizes under 500 LOC.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger, get_tracer
from provide.uterm.bridge.hub.ext import (
    EVENT_HIJACK_ACQUIRED,
    EVENT_HIJACK_EXPIRED,
    EVENT_HIJACK_RELEASED,
)
from provide.uterm.bridge.models import HijackSession

if TYPE_CHECKING:
    import asyncio

    from fastapi import WebSocket

    from provide.uterm.bridge.models import WorkerTermState

logger = get_logger(__name__)
tracer = get_tracer(__name__)


class _OwnershipMixin:
    r"""Mixin providing hijack ownership/lease methods for TermHub.

    Requires the host class to provide: \`\`_lock\`\`, \`\`_workers\`\`,
    \`\`_dashboard_hijack_lease_s\`\`, \`\`is_hijacked\`\`, \`\`is_dashboard_hijack_active\`\`,
    \`\`has_valid_rest_lease\`\`, \`\`send_worker\`\`, \`\`broadcast_hijack_state\`\`,
    \`\`append_event\`\`, \`\`prune_if_idle\`\`, \`\`notify_hijack_changed\`\`.
    """

    # -- Typed self helpers (avoid repeating the cast everywhere) ---------------
    _lock: asyncio.Lock
    _workers: dict[str, WorkerTermState]
    _dashboard_hijack_lease_s: int

    @staticmethod
    def _compute_lease_expirations(st: Any, now: float) -> tuple[bool, bool]:
        r"""Return \`\`(browser_expired, rest_expired)\`\` without mutating state.

        *browser_expired* is True when the dashboard WS owner lease has lapsed.
        *rest_expired* is True when the REST hijack session lease has lapsed.
        """
        rest_expired = st.hijack_session is not None and st.hijack_session.lease_expires_at <= now
        browser_expired = (
            st.hijack_owner is not None and st.hijack_owner_expires_at is not None and st.hijack_owner_expires_at <= now
        )
        return browser_expired, rest_expired

    async def _expire_leases_under_lock(self, worker_id: str, now: float) -> tuple[bool, bool, bool] | None:
        """Expire stale leases under lock; returns (rest_expired, dashboard_expired, should_resume) or None."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return None
            if st.hijack_session is None and st.hijack_owner is None:
                return None
            browser_expired, rest_expired = self._compute_lease_expirations(st, now)
            dashboard_expired = browser_expired
            if rest_expired:
                st.hijack_session = None
            if dashboard_expired:
                st.hijack_owner = None
                st.hijack_owner_expires_at = None
            should_resume = (
                (rest_expired or dashboard_expired) and st.hijack_owner is None and st.hijack_session is None
            )
        return rest_expired, dashboard_expired, should_resume

    async def _recheck_and_resume(self, worker_id: str, now: float) -> None:
        """Re-check under lock that no concurrent hijack was acquired; send resume if clear."""
        async with self._lock:
            st2 = self._workers.get(worker_id)
            if st2 is not None and self.is_hijacked(st2):  # type: ignore[attr-defined]  # pragma: no branch
                return
        await self.send_worker(  # type: ignore[attr-defined]
            worker_id,
            {"type": "control", "action": "resume", "owner": "lease-expired", "lease_s": 0, "ts": now},
        )
        self.notify_hijack_changed(worker_id, enabled=False, owner=None)  # type: ignore[attr-defined]

    async def cleanup_expired_hijack(self, worker_id: str) -> bool:
        """Expire any stale REST or dashboard leases for *worker_id*; send resume if fully released."""
        now = time.monotonic()
        result = await self._expire_leases_under_lock(worker_id, now)
        if result is None:
            return False
        rest_expired, dashboard_expired, should_resume = result
        if not rest_expired and not dashboard_expired:
            return False
        self.metric("hijack_lease_expiries_total")  # type: ignore[attr-defined]
        if should_resume:
            await self._recheck_and_resume(worker_id, now)
        if rest_expired:
            await self.append_event(worker_id, "hijack_lease_expired")  # type: ignore[attr-defined]
            logger.info(EVENT_HIJACK_EXPIRED, worker_id=worker_id, hijack_type="rest")
        if dashboard_expired:
            await self.append_event(worker_id, "hijack_owner_expired")  # type: ignore[attr-defined]
            logger.info(EVENT_HIJACK_EXPIRED, worker_id=worker_id, hijack_type="dashboard")
        await self.broadcast_hijack_state(worker_id)  # type: ignore[attr-defined]
        await self.prune_if_idle(worker_id)  # type: ignore[attr-defined]
        return True

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> HijackSession | None:
        """Return the active REST hijack session matching *hijack_id*, or None if expired/missing."""
        await self.cleanup_expired_hijack(worker_id)
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return None
            hs = st.hijack_session
            if hs is None or hs.lease_expires_at <= time.monotonic() or hs.hijack_id != hijack_id:
                return None
            return hs

    async def try_acquire_rest_hijack(
        self,
        worker_id: str,
        *,
        owner: str,
        lease_s: int,
        hijack_id: str,
        now: float,
    ) -> tuple[bool, str | None]:
        r"""Atomically check availability and create a REST hijack session.

        Returns:
            \`\`(True, None)\`\` on success.
            \`\`(False, "no_worker")\`\` if the worker disconnected before the lock.
            \`\`(False, "already_hijacked")\`\` if another session is active.
        """
        with tracer.start_as_current_span(
            "uterm.hijack.acquire.rest", attributes={"worker_id": worker_id, "owner": owner}
        ):
            async with self._lock:
                st = self._workers.get(worker_id)
                if st is None or st.worker_ws is None:
                    return False, "no_worker"
                if st.input_mode == "open":
                    return False, "open_mode"
                if self.is_dashboard_hijack_active(st) or self.has_valid_rest_lease(st):  # type: ignore[attr-defined]
                    return False, "already_hijacked"

                # Send pause while holding the lock to ensure the worker is notified
                # atomically with the session creation.
                try:
                    from provide.uterm.bridge.hub.core import _encode_worker_frame

                    await st.worker_ws.send_text(
                        _encode_worker_frame(
                            {
                                "type": "control",
                                "action": "pause",
                                "owner": owner,
                                "hijack_id": hijack_id,
                                "ts": time.time(),
                            }
                        )
                    )
                except Exception as exc:
                    logger.debug("pause_worker_failed worker_id=%s: %s", worker_id, exc)
                    st.worker_ws = None
                    return False, "no_worker"

                st.hijack_session = HijackSession(
                    hijack_id=hijack_id,
                    owner=owner,
                    acquired_at=now,
                    lease_expires_at=now + lease_s,
                    last_heartbeat=now,
                )
            logger.info(EVENT_HIJACK_ACQUIRED, worker_id=worker_id, hijack_type="rest", owner=owner, lease_s=lease_s)
            return True, None

    async def try_acquire_ws_hijack(self, worker_id: str, ws: WebSocket) -> tuple[bool, str | None]:
        r"""Atomically check availability and set the dashboard WS hijack owner.

        Returns:
            \`\`(True, None)\`\` on success.
            \`\`(False, "no_worker")\`\` if no worker is connected.
            \`\`(False, "already_hijacked")\`\` if another hijack is active.
        """
        with tracer.start_as_current_span("uterm.hijack.acquire.ws", attributes={"worker_id": worker_id}):
            async with self._lock:
                st = self._workers.get(worker_id)
                if st is None or st.worker_ws is None:
                    return False, "no_worker"
                if self.is_dashboard_hijack_active(st) or self.has_valid_rest_lease(st):  # type: ignore[attr-defined]
                    return False, "already_hijacked"
                ttl = self._dashboard_hijack_lease_s
                st.hijack_owner = ws
                st.hijack_owner_expires_at = time.monotonic() + ttl
            logger.info(EVENT_HIJACK_ACQUIRED, worker_id=worker_id, hijack_type="dashboard", lease_s=ttl)
            return True, None

    async def touch_hijack_owner(self, worker_id: str, lease_s: int | None = None) -> float | None:
        """Extend the dashboard WS hijack lease; returns new expiry timestamp or None if no owner."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or st.hijack_owner is None:
                return None
            ttl = self._dashboard_hijack_lease_s if lease_s is None else max(1, min(int(lease_s), 600))
            st.hijack_owner_expires_at = time.monotonic() + ttl
            return st.hijack_owner_expires_at

    async def touch_if_owner(self, worker_id: str, ws: WebSocket) -> float | None:
        """Atomically verify WS ownership and extend lease; returns new expiry or None."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or not self.is_dashboard_hijack_active(st) or st.hijack_owner is not ws:  # type: ignore[attr-defined]
                return None
            st.hijack_owner_expires_at = time.monotonic() + self._dashboard_hijack_lease_s
            return st.hijack_owner_expires_at

    async def try_release_ws_hijack(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        r"""Atomically verify ownership and clear it in a single lock block.

        Returns:
            \`\`(released, rest_active)\`\` where *released* is \`\`True\`\` if *ws*
            was the active dashboard hijack owner and was cleared.
        """
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or not self.is_dashboard_hijack_active(st) or st.hijack_owner is not ws:  # type: ignore[attr-defined]
                rest_active = st is not None and self.has_valid_rest_lease(st)  # type: ignore[attr-defined]
                return False, rest_active
            st.hijack_owner = None
            st.hijack_owner_expires_at = None
            rest_active = self.has_valid_rest_lease(st)  # type: ignore[attr-defined]
        logger.info(EVENT_HIJACK_RELEASED, worker_id=worker_id, hijack_type="dashboard")
        return True, rest_active

    async def remove_dead_browsers(self, worker_id: str, dead: set[WebSocket]) -> bool:
        r"""Remove *dead* browser sockets from worker state under lock.

        If the dashboard hijack owner was among the dead sockets, clears the
        lease and sends a resume control frame (unless a REST session is still
        active).

        Returns \`\`True\`\` if the hijack state changed (owner cleared and resumed).
        """
        notify_hijack_off = False
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is not None:
                for ws in dead:
                    st.browsers.pop(ws, None)
                    if self.is_dashboard_hijack_active(st) and st.hijack_owner is ws:  # type: ignore[attr-defined]
                        st.hijack_owner = None
                        st.hijack_owner_expires_at = None
                        notify_hijack_off = not self.has_valid_rest_lease(st)  # type: ignore[attr-defined]
        if notify_hijack_off:
            # Re-check: a concurrent acquire may have written a new session
            # between the lock release above and _send_worker below.
            async with self._lock:
                _st2 = self._workers.get(worker_id)
                if _st2 is not None and self.is_hijacked(_st2):  # type: ignore[attr-defined]  # pragma: no branch
                    notify_hijack_off = False
        if notify_hijack_off:
            await self.send_worker(  # type: ignore[attr-defined]
                worker_id,
                {"type": "control", "action": "resume", "owner": "dead-socket", "lease_s": 0, "ts": time.time()},
            )
            self.notify_hijack_changed(worker_id, enabled=False, owner=None)  # type: ignore[attr-defined]
        return notify_hijack_off

    async def extend_hijack_lease(
        self, worker_id: str, hijack_id: str, owner: str, lease_s: int, now: float
    ) -> float | None:
        """Extend the REST hijack lease. Returns the new expiry or None if the session is not found or owner mismatch."""
        with tracer.start_as_current_span(
            "uterm.hijack.heartbeat", attributes={"worker_id": worker_id, "owner": owner}
        ):
            async with self._lock:
                st = self._workers.get(worker_id)
                if st is None or st.hijack_session is None or st.hijack_session.hijack_id != hijack_id:
                    return None
                if st.hijack_session.owner != owner:
                    logger.warning(
                        "hijack_heartbeat_denied_owner_mismatch worker_id=%s hijack_id=%s current=%s attempted=%s",
                        worker_id,
                        hijack_id,
                        st.hijack_session.owner,
                        owner,
                    )
                    self.metric("hijack_heartbeat_denied_owner_mismatch")  # type: ignore[attr-defined]
                    return None
                st.hijack_session.last_heartbeat = now
                st.hijack_session.lease_expires_at = now + lease_s
                return st.hijack_session.lease_expires_at

    async def get_fresh_hijack_expiry(self, worker_id: str, hijack_id: str, fallback: float) -> float:
        """Re-read the current lease expiry under lock (a concurrent heartbeat may have extended it)."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is not None and st.hijack_session is not None and st.hijack_session.hijack_id == hijack_id:
                return st.hijack_session.lease_expires_at
        return fallback

    async def get_hijack_events_data(
        self,
        worker_id: str,
        hijack_id: str,
        hs: HijackSession,
        after_seq: int,
        limit: int,
    ) -> dict[str, Any]:
        """Return the events payload for a REST hijack events endpoint (under lock)."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:  # pragma: no cover
                return {
                    "rows": [],
                    "latest_seq": 0,
                    "min_event_seq": 0,
                    "fresh_expires": hs.lease_expires_at,
                }
            rows = [evt for evt in list(st.events) if int(evt.get("seq", 0)) > after_seq][:limit]
            latest_seq = st.event_seq
            min_event_seq = st.min_event_seq
            fresh_expires = (
                st.hijack_session.lease_expires_at
                if st.hijack_session is not None and st.hijack_session.hijack_id == hijack_id
                else hs.lease_expires_at
            )
        return {
            "rows": rows,
            "latest_seq": latest_seq,
            "min_event_seq": min_event_seq,
            "fresh_expires": fresh_expires,
        }

    async def check_hijack_valid(self, worker_id: str, hijack_id: str) -> bool:
        """Return True if the REST hijack session is still valid (checked under lock)."""
        async with self._lock:
            st = self._workers.get(worker_id)
            return (
                st is not None
                and st.hijack_session is not None
                and st.hijack_session.hijack_id == hijack_id
                and st.hijack_session.lease_expires_at > time.monotonic()
            )

    async def release_rest_hijack(self, worker_id: str, hijack_id: str) -> tuple[bool, bool]:
        r"""Atomically clear the REST hijack session.

        Returns:
            \`\`(was_released, should_resume)\`\` — *should_resume* is True if no other hijack is active.
        """
        with tracer.start_as_current_span("uterm.hijack.release", attributes={"worker_id": worker_id}):
            async with self._lock:
                st = self._workers.get(worker_id)
                if st is None or st.hijack_session is None or st.hijack_session.hijack_id != hijack_id:
                    return False, False
                st.hijack_session = None
                should_resume = not self.is_dashboard_hijack_active(st)  # type: ignore[attr-defined]
            return True, should_resume

    async def check_still_hijacked(self, worker_id: str) -> bool:
        """Return True if any hijack (REST or dashboard WS) is currently active."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return False
            return bool(self.is_hijacked(st))  # type: ignore[attr-defined]

    async def is_input_open_mode(self, worker_id: str) -> bool:
        """Return True if the worker is in open input mode."""
        async with self._lock:
            st = self._workers.get(worker_id)
            return st is not None and st.input_mode == "open"

    async def prepare_browser_input(self, worker_id: str, ws: WebSocket) -> bool:
        """Check if ws may send input; also extend the dashboard lease if ws is the owner.

        Returns True if the browser is allowed to send input to the worker.
        """
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return False
            allowed: bool = bool(self.can_send_input(st, ws))  # type: ignore[attr-defined]
            if self.is_dashboard_hijack_active(st) and st.hijack_owner is ws:  # type: ignore[attr-defined]
                st.hijack_owner_expires_at = time.monotonic() + self._dashboard_hijack_lease_s
            return allowed


_HijackOwnershipMixin = _OwnershipMixin
