#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Lease-delegate bodies extracted from :class:`TermHub`.

``self.lease`` (:class:`HijackLeaseManager`) owns the hijack state
machine. The :class:`TermHub` methods that wrap a lease call *and* emit
lifecycle telemetry (or do a pre-cleanup snapshot) keep a one-line
wrapper on the class — preserving the no-mixin ``hub.<name>(...)`` call
surface — while their bodies live here as module-level functions taking
``hub`` as the first argument. Tests that monkey-patch
``emit_telemetry`` / ``cleanup_expired_hijack`` on a hub instance still
intercept because these functions dispatch through ``hub.<method>``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.server.bridge.hub.core_impl import TermHub
    from provide.uterm.server.bridge.models import HijackSession


async def cleanup_expired_hijack(hub: TermHub, worker_id: str) -> bool:
    """Expire stale REST/dashboard leases; emit resume if fully released."""
    # Capture a pre-cleanup snapshot (lock-free; telemetry is fail-open)
    # so we know which hijack_type(s) to report when cleanup returns True.
    now = time.monotonic()
    st = hub.registry._workers.get(worker_id)
    had_rest = st is not None and st.hijack_session is not None and st.hijack_session.lease_expires_at <= now
    had_dashboard = (
        st is not None
        and st.hijack_owner is not None
        and st.hijack_owner_expires_at is not None
        and st.hijack_owner_expires_at <= now
    )
    cleaned = await hub.lease.cleanup_expired(worker_id)
    if cleaned:
        if had_rest:
            await hub.emit_telemetry("hijack.expired", worker_id=worker_id, metadata={"hijack_type": "rest"})
        if had_dashboard:
            await hub.emit_telemetry("hijack.expired", worker_id=worker_id, metadata={"hijack_type": "dashboard"})
    return cleaned


async def get_rest_session(hub: TermHub, worker_id: str, hijack_id: str) -> HijackSession | None:
    """Return the active REST session for *hijack_id* or None."""
    await hub.cleanup_expired_hijack(worker_id)
    return await hub.lease._get_rest_session_no_cleanup(worker_id, hijack_id)


async def try_acquire_rest_hijack(
    hub: TermHub,
    worker_id: str,
    *,
    owner: str,
    lease_s: int,
    hijack_id: str,
    now: float,
) -> tuple[bool, str | None]:
    """Atomically check availability and create a REST hijack session."""
    ok, err = await hub.lease.try_acquire_rest(worker_id, owner=owner, lease_s=lease_s, hijack_id=hijack_id, now=now)
    if ok:
        await hub.emit_telemetry(
            "hijack.acquired",
            worker_id=worker_id,
            principal=owner,
            metadata={"hijack_type": "rest", "lease_s": lease_s},
        )
    return ok, err


async def try_acquire_ws_hijack(hub: TermHub, worker_id: str, ws: WebSocket) -> tuple[bool, str | None]:
    """Atomically check availability and set the dashboard WS hijack owner."""
    ok, err = await hub.lease.try_acquire_ws(worker_id, ws)
    if ok:
        await hub.emit_telemetry(
            "hijack.acquired",
            worker_id=worker_id,
            metadata={"hijack_type": "dashboard", "lease_s": hub.lease.dashboard_hijack_lease_s},
        )
    return ok, err


async def try_release_ws_hijack(hub: TermHub, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
    """Atomically verify ownership and clear in a single lock block."""
    ok, rest_active = await hub.lease.try_release_ws(worker_id, ws)
    if ok:
        await hub.emit_telemetry("hijack.released", worker_id=worker_id, metadata={"hijack_type": "dashboard"})
    return ok, rest_active


__all__ = [
    "cleanup_expired_hijack",
    "get_rest_session",
    "try_acquire_rest_hijack",
    "try_acquire_ws_hijack",
    "try_release_ws_hijack",
]
