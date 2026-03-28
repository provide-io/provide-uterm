from __future__ import annotations

import time
import uuid
from dataclasses import dataclass


@dataclass(slots=True)
class HijackSession:
    hijack_id: str
    owner: str
    lease_expires_at: float


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
        return self._active_session(time.time())

    def acquire(self, owner: str, lease_s: int, *, now: float | None = None) -> AcquireResult:
        """Acquire a hijack lease, always generating a new hijack_id.

        If the same *owner* already holds the lease the lease is renewed with a
        fresh ``hijack_id`` so callers always receive an authoritative token for
        the new lease period.  A different owner while a lease is active returns
        ``ok=False``.
        """
        now_ts = time.time() if now is None else now
        active = self._active_session(now_ts)
        if active is not None and active.owner != owner:
            return AcquireResult(ok=False, session=active, error="already_hijacked")
        is_renewal = active is not None  # same owner renewing an existing lease
        expires_at = now_ts + max(1, min(int(lease_s), 3600))
        active = HijackSession(hijack_id=str(uuid.uuid4()), owner=owner, lease_expires_at=expires_at)
        self._session = active
        return AcquireResult(ok=True, session=active, is_renewal=is_renewal)

    def heartbeat(self, hijack_id: str, lease_s: int, *, now: float | None = None) -> AcquireResult:
        now_ts = time.time() if now is None else now
        active = self._active_session(now_ts)
        if active is None:
            return AcquireResult(ok=False, session=None, error="not_hijacked")
        if active.hijack_id != hijack_id:
            return AcquireResult(ok=False, session=active, error="hijack_id_mismatch")
        active.lease_expires_at = now_ts + max(1, min(int(lease_s), 3600))
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
