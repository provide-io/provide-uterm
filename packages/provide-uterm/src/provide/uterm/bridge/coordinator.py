#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Single-session hijack coordinator — shared between FastAPI and CF backends.

Provides the core lease arbitration state machine: acquire → heartbeat → release.
The FastAPI ``_HijackOwnershipMixin`` wraps this with async locking and multi-worker
management; the CF Durable Object uses it directly as a single-writer coordinator.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass


def _clamp_lease(lease_s: int) -> int:
    """Clamp lease duration to [1, 3600] seconds."""
    return max(1, min(int(lease_s), 3600))


@dataclass(slots=True)
class HijackSession:
    """A live hijack lease."""

    hijack_id: str
    owner: str
    lease_expires_at: float
    acquired_at: float = 0.0
    last_heartbeat: float = 0.0
    # Authenticated subject_id of the principal that acquired this lease (REST
    # path). Distinct from ``owner`` (a self-declared display label): used to
    # verify lease ownership at release. None for unauthenticated/legacy leases.
    acquired_by: str | None = None
    # Highest snapshot ``event_seq`` handed to this lease. Freshness for a
    # polling client means "newer than what I was last served"; comparing
    # against the request's wall clock instead discards a frame that was pushed
    # microseconds earlier, which is exactly the frame the caller wants.
    last_served_event_seq: int = 0


@dataclass(slots=True)
class AcquireResult:
    ok: bool
    session: HijackSession | None
    error: str | None = None
    is_renewal: bool = False  # True when the same owner renewed an existing lease


class HijackCoordinator:
    """Single-writer hijack arbitration for one worker session."""

    def __init__(self) -> None:
        self._session: HijackSession | None = None

    def _active_session(self, now_ts: float) -> HijackSession | None:
        session = self._session
        if session is None:
            return None
        if session.lease_expires_at <= now_ts:
            self._session = None
            return None
        return session

    @property
    def session(self) -> HijackSession | None:
        """Return the live lease without mutating coordinator state.

        Expiry is a state transition and therefore belongs in an explicitly
        serialized operation (``acquire``, ``heartbeat``, or a backend expiry
        sweep).  Keeping this property observational prevents an innocent
        status read from clearing ownership outside a backend's state guard.
        """
        session = self._session
        if session is None or session.lease_expires_at <= time.monotonic():
            return None
        return session

    def acquire(self, owner: str, lease_s: int, *, now: float | None = None) -> AcquireResult:
        """Acquire a hijack lease, always generating a new hijack_id.

        If the same *owner* already holds the lease the lease is renewed with a
        fresh ``hijack_id`` so callers always receive an authoritative token for
        the new lease period.  A different owner while a lease is active returns
        ``ok=False``.
        """
        now_ts = time.monotonic() if now is None else now
        active = self._active_session(now_ts)
        if active is not None and active.owner != owner:
            return AcquireResult(ok=False, session=active, error="already_hijacked")
        is_renewal = active is not None  # same owner renewing an existing lease
        expires_at = now_ts + _clamp_lease(lease_s)
        active = HijackSession(
            hijack_id=str(uuid.uuid4()),
            owner=owner,
            lease_expires_at=expires_at,
            acquired_at=now_ts,
            last_heartbeat=now_ts,
        )
        self._session = active
        return AcquireResult(ok=True, session=active, is_renewal=is_renewal)

    def heartbeat(
        self,
        hijack_id: str,
        lease_s: int,
        owner: str | None = None,
        *,
        now: float | None = None,
    ) -> AcquireResult:
        now_ts = time.monotonic() if now is None else now
        active = self._active_session(now_ts)
        if active is None:
            return AcquireResult(ok=False, session=None, error="not_hijacked")
        if active.hijack_id != hijack_id:
            return AcquireResult(ok=False, session=active, error="hijack_id_mismatch")
        if owner is not None and active.owner != owner:
            return AcquireResult(ok=False, session=active, error="owner_mismatch")
        active.lease_expires_at = now_ts + _clamp_lease(lease_s)
        active.last_heartbeat = now_ts
        return AcquireResult(ok=True, session=active)

    def release(self, hijack_id: str) -> AcquireResult:
        active = self._session
        if active is None:
            return AcquireResult(ok=False, session=None, error="not_hijacked")
        if active.hijack_id != hijack_id:
            return AcquireResult(ok=False, session=active, error="hijack_id_mismatch")
        self._session = None
        return AcquireResult(ok=True, session=None)

    def can_send_input(self, hijack_id: str | None) -> bool:
        active = self.session
        if active is None:
            return False
        return hijack_id == active.hijack_id
