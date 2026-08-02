#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hijack-clearing lifecycle helpers for :class:`ConnectionManager`.

Holds the *body* of the three hijack-clearing methods that previously lived
inline on :class:`provide.uterm.server.bridge.hub.connection.ConnectionManager`:
``disconnect_worker`` (programmatic worker WS teardown + cleared-hijack
broadcast), its ``_event_bus_close`` close helper, and ``force_release_hijack``
(forcibly clears any active hijack and emits the follow-up ``resume`` control
frame). These bodies are split out as module-level functions taking the manager
(``mgr``) as their first argument so the line bulk — and, more importantly, the
*mutants* — move to this module while the public methods stay on
:class:`ConnectionManager` as thin one-line wrappers (the class keeps zero mixin
parents and an unchanged import surface).

This module is in the mutation perimeter at killed==100: the
``tests/bridge/hub/test_connection_kill_hijack_clearing.py`` and
``test_connection_kill_ratelimit_force.py`` suites drive the
:class:`ConnectionManager` methods by name, so every return value, state
mutation, and observability call here is pinned. The kill-suites patch this
module's ``logger`` (via ``connection_hijack.logger``) to assert the
``disconnect_worker`` close-error debug log.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, cast

from provide.telemetry import get_logger

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.server.bridge.hub.connection import ConnectionManager

logger = get_logger(__name__)


async def disconnect_worker(mgr: ConnectionManager, worker_id: str) -> bool:
    """Body of :meth:`ConnectionManager.disconnect_worker`.

    Programmatically disconnect the worker WS. Returns ``True`` if a worker was
    connected.

    Inter-step hooks (``broadcast``, ``notify_hijack_changed``,
    ``broadcast_hijack_state``, ``prune_if_idle``) are dispatched via
    ``mgr._hub.<method>`` so existing mutation-killing tests which patch the
    hub-level names continue to intercept them after the orchestration moved
    into the service. The hub-level methods are pure one-line delegators back to
    their owning services, so the cycle terminates on the second hop.
    """
    from provide.uterm.server.bridge.frames import make_worker_disconnected_frame

    hub = mgr._hub
    ws: WebSocket | None = None
    async with hub._lock:
        st = hub.registry.get(worker_id)
        if st is None or st.worker_ws is None:
            return False
        state = st
        fence = st.owned_input_fence
    async with fence:
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is not state or st.worker_ws is None:
                return False
            ws = st.worker_ws
            st.worker_ws = None
            was_hijacked = st.hijack_session is not None or st.hijack_owner is not None
            st.hijack_session = None
            st.hijack_owner = None
            st.hijack_owner_expires_at = None
            st.ownership_generation += 1
    try:
        await ws.close()
    except Exception as exc:
        logger.debug("disconnect_worker close error worker_id=%s: %s", worker_id, exc)
    await hub.broadcast(worker_id, cast("dict[str, Any]", make_worker_disconnected_frame(worker_id)))
    if was_hijacked:
        hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
        await hub.broadcast_hijack_state(worker_id)
    if hub._event_bus is not None:
        # Call through the wrapper method (not the module function) so the
        # existing kill-suite patch of ``ConnectionManager._event_bus_close``
        # still intercepts this guarded call.
        mgr._event_bus_close(worker_id)
    await hub.prune_if_idle(worker_id)
    return True


def _event_bus_close(mgr: ConnectionManager, worker_id: str) -> None:
    """Body of :meth:`ConnectionManager._event_bus_close`.

    Indirect close so the EventBus reference is read on the hub each call.
    Tests substitute ``hub._event_bus`` after construction; reading through the
    back-reference keeps that pattern working without plumbing a setter through
    this service.
    """
    bus = mgr._hub._event_bus
    if bus is not None:  # pragma: no branch
        bus.close_worker(worker_id)
    operation_bus = mgr._hub._operation_event_bus
    if operation_bus is not None:
        operation_bus.close_worker(worker_id)


async def force_release_hijack(mgr: ConnectionManager, worker_id: str) -> bool:
    """Body of :meth:`ConnectionManager.force_release_hijack`.

    Forcibly clear any active hijack for *worker_id* and send a resume control
    frame. Returns ``True`` if a hijack was active and was cleared, ``False``
    otherwise. Typically called before switching input mode to ``"open"`` or on
    session teardown.
    """
    hub = mgr._hub
    owner = "server-forced"
    had_hijack = False
    async with hub._lock:
        st = hub.registry.get(worker_id)
        if st is None:
            return False
        state = st
        fence = st.owned_input_fence
    async with fence:
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is not state:
                return False
            if st.hijack_session is not None:
                owner = st.hijack_session.owner
                st.hijack_session = None
                had_hijack = True
            if hub.is_dashboard_hijack_active(st):  # pragma: no branch
                st.hijack_owner = None
                st.hijack_owner_expires_at = None
                had_hijack = True
            # Same Python-3.11 coverage.py async-with __aexit__ arc quirk as in
            # HubConnectionService.set_worker_hello: the False arc leaves the
            # `async with hub._lock` block for the `if not had_hijack` below, and
            # 3.11 mis-attributes that crossing. 3.12+ records it correctly. The
            # branch IS exercised — see
            # test_known_worker_no_hijack_returns_false_no_side_effects.
            if had_hijack:  # pragma: no branch
                st.ownership_generation += 1
        if not had_hijack:
            return False
        try:
            await asyncio.wait_for(
                hub.send_worker(
                    worker_id,
                    {"type": "control", "action": "resume", "owner": owner, "lease_s": 0, "ts": time.time()},
                ),
                timeout=5.0,
            )
        except TimeoutError:
            logger.warning("force_release_resume_timeout worker_id=%s", worker_id)
    hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
    await hub.broadcast_hijack_state(worker_id)
    return True


__all__ = ["_event_bus_close", "disconnect_worker", "force_release_hijack"]
