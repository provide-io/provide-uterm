#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hijack ownership/lease mixin — thin shim forwarding to ``HijackLeaseManager``.

The hijack lease state machine moved to
:class:`provide.uterm.bridge.hub.lease.HijackLeaseManager` (Phase 4 of
the architecture refactor). This mixin survives as a back-compat
forwarder so the existing TermHub public surface and the cooperative
super-call chain through ``HubMessagingMixin.remove_dead_browsers``
keep working unchanged; new code should call into ``self.lease``
directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.bridge.hub.lease import HijackLeaseManager
    from provide.uterm.bridge.models import HijackSession

logger = get_logger(__name__)


class _OwnershipMixin:
    """Mixin exposing the legacy hijack-ownership method names on TermHub.

    Every method forwards to the composed :class:`HijackLeaseManager`
    instance (``self.lease``) which holds the actual state machine.
    Keeping the names on the mixin preserves the cooperative MRO
    super-call chain used by :class:`HubMessagingMixin` for
    ``remove_dead_browsers``.
    """

    # Typed handle to the composed service; set by ``TermHub.__init__``.
    lease: HijackLeaseManager

    # -- Static helper kept on the class for legacy import paths -----------

    @staticmethod
    def _compute_lease_expirations(st: Any, now: float) -> tuple[bool, bool]:
        """Return ``(browser_expired, rest_expired)`` without mutating state."""
        from provide.uterm.bridge.hub.lease import HijackLeaseManager

        return HijackLeaseManager.compute_lease_expirations(st, now)

    # -- Private helpers preserved for mutation-test patchability ---------
    # These delegate to ``self.lease`` but stay on the mixin so existing
    # mutation tests that swap them via ``hub._recheck_and_resume = ...``
    # continue to intercept. Top-level methods like
    # :meth:`cleanup_expired_hijack` route through these shims (rather
    # than calling the lease service directly) so a test patching
    # ``hub._recheck_and_resume`` still sees its mock invoked.

    async def _expire_leases_under_lock(self, worker_id: str, now: float) -> tuple[bool, bool, bool] | None:
        """Expire stale leases under lock — forwards to :attr:`lease`."""
        return await self.lease._expire_leases_under_lock(worker_id, now)

    async def _recheck_and_resume(self, worker_id: str, now: float) -> None:
        """Re-check under lock and send resume — forwards to :attr:`lease`."""
        await self.lease._recheck_and_resume(worker_id, now)

    # -- Expiry sweep ------------------------------------------------------

    async def cleanup_expired_hijack(self, worker_id: str) -> bool:
        """Expire stale REST/dashboard leases; emit resume if fully released.

        Reimplemented at the mixin level (rather than forwarding straight
        to :attr:`lease`) so mutation tests that patch
        ``hub._recheck_and_resume`` continue to intercept the call path.
        Logic mirrors :meth:`HijackLeaseManager.cleanup_expired`.
        """
        import time as _time

        from provide.uterm.bridge.hub.ext import EVENT_HIJACK_EXPIRED
        from provide.uterm.bridge.hub.lease import logger as _lease_logger

        now = _time.monotonic()
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
            _lease_logger.info(EVENT_HIJACK_EXPIRED, worker_id=worker_id, hijack_type="rest")
        if dashboard_expired:
            await self.append_event(worker_id, "hijack_owner_expired")  # type: ignore[attr-defined]
            _lease_logger.info(EVENT_HIJACK_EXPIRED, worker_id=worker_id, hijack_type="dashboard")
        await self.broadcast_hijack_state(worker_id)  # type: ignore[attr-defined]
        await self.prune_if_idle(worker_id)  # type: ignore[attr-defined]
        return True

    # -- REST session lookup ----------------------------------------------

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> HijackSession | None:
        """Return the active REST session for *hijack_id* or None.

        Routes the expiry sweep through ``self.cleanup_expired_hijack``
        (rather than calling the lease service directly) so mutation
        tests that patch the public method still see the invocation.
        """
        await self.cleanup_expired_hijack(worker_id)
        return await self.lease._get_rest_session_no_cleanup(worker_id, hijack_id)

    # -- Acquire -----------------------------------------------------------

    async def try_acquire_rest_hijack(
        self,
        worker_id: str,
        *,
        owner: str,
        lease_s: int,
        hijack_id: str,
        now: float,
    ) -> tuple[bool, str | None]:
        """Atomically check availability and create a REST hijack session."""
        return await self.lease.try_acquire_rest(worker_id, owner=owner, lease_s=lease_s, hijack_id=hijack_id, now=now)

    async def try_acquire_ws_hijack(self, worker_id: str, ws: WebSocket) -> tuple[bool, str | None]:
        """Atomically check availability and set the dashboard WS hijack owner."""
        return await self.lease.try_acquire_ws(worker_id, ws)

    # -- Touch / heartbeat -------------------------------------------------

    async def touch_hijack_owner(self, worker_id: str, lease_s: int | None = None) -> float | None:
        """Extend the dashboard WS hijack lease."""
        return await self.lease.touch_owner(worker_id, lease_s)

    async def touch_if_owner(self, worker_id: str, ws: WebSocket) -> float | None:
        """Atomically verify WS ownership and extend lease."""
        return await self.lease.touch_if_owner(worker_id, ws)

    # -- Release -----------------------------------------------------------

    async def try_release_ws_hijack(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        """Atomically verify ownership and clear in a single lock block."""
        return await self.lease.try_release_ws(worker_id, ws)

    async def remove_dead_browsers(self, worker_id: str, dead: set[WebSocket]) -> bool:
        """Remove *dead* browser sockets under lock; resume if owner was dead."""
        return await self.lease.remove_dead_browsers(worker_id, dead)

    async def extend_hijack_lease(
        self, worker_id: str, hijack_id: str, owner: str, lease_s: int, now: float
    ) -> float | None:
        """Extend the REST hijack lease."""
        return await self.lease.extend_lease(worker_id, hijack_id, owner, lease_s, now)

    async def get_fresh_hijack_expiry(self, worker_id: str, hijack_id: str, fallback: float) -> float:
        """Re-read the current lease expiry under lock."""
        return await self.lease.get_fresh_expiry(worker_id, hijack_id, fallback)

    async def get_hijack_events_data(
        self,
        worker_id: str,
        hijack_id: str,
        hs: HijackSession,
        after_seq: int,
        limit: int,
    ) -> dict[str, Any]:
        """Return the events payload for a REST hijack events endpoint."""
        return await self.lease.get_events_data(worker_id, hijack_id, hs, after_seq, limit)

    async def check_hijack_valid(self, worker_id: str, hijack_id: str) -> bool:
        """Return True if the REST hijack session is still valid."""
        return await self.lease.check_valid(worker_id, hijack_id)

    async def release_rest_hijack(self, worker_id: str, hijack_id: str) -> tuple[bool, bool]:
        """Atomically clear the REST hijack session."""
        return await self.lease.release_rest(worker_id, hijack_id)

    async def check_still_hijacked(self, worker_id: str) -> bool:
        """Return True if any hijack (REST or dashboard WS) is currently active."""
        return await self.lease.still_hijacked(worker_id)

    async def is_input_open_mode(self, worker_id: str) -> bool:
        """Return True if the worker is in open input mode."""
        return await self.lease.is_input_open_mode(worker_id)

    async def prepare_browser_input(self, worker_id: str, ws: WebSocket) -> bool:
        """Check if ws may send input; extends dashboard lease if ws is owner."""
        return await self.lease.prepare_browser_input(worker_id, ws)


_HijackOwnershipMixin = _OwnershipMixin
