#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""StateStore: input-buffer + lifecycle/policy helpers extracted from ``HubStateMixin``.

Phase 7 of refactor #16 finishes the TermHub mixin teardown by lifting
the remaining ``HubStateMixin`` responsibilities into a service class
that the hub composes as ``self.state``. Same shape as
:class:`HijackLeaseManager`, :class:`MessageRouter`,
:class:`ConnectionManager` and :class:`PresenceManager`: a single back
reference to the composing :class:`TermHub`, no behavioural changes, no
lock semantics changes.

Scope:

* Per-browser line-buffering helper (``buffer_and_get_command``).
* Worker-state ``touch_activity`` heartbeat and ``get_or_create``
  helper (was ``_get``).
* Hijack-state predicates (``has_valid_rest_lease``,
  ``is_dashboard_hijack_active``, ``is_hijacked``) — small pure
  functions that the lease manager and the messaging router both call.
* The ``notify_hijack_changed`` and ``metric`` callback fan-out.
* Browser-role resolution (``resolve_role_for_browser``) and the
  identity-provider plumbing for ``prepare_policy_context``.
* Graceful task shutdown (``shutdown``).
* The ``clamp_lease`` cap.

The static helpers (``clamp_lease``, ``has_valid_rest_lease``,
``is_dashboard_hijack_active``) stay as ``@staticmethod`` so the mixin
shim can re-expose them via ``staticmethod(StateStore.x)`` without
allocating a bound method on every hub instance.

Lock semantics are intentionally preserved verbatim: the store uses the
*hub's* ``asyncio.Lock`` (accessed via the back reference) so concurrent
state mutations keep serialising against the same object the rest of
the hub uses.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import TYPE_CHECKING

from provide.telemetry import get_logger
from provide.uterm.bridge.hub.ext import PolicyContext
from provide.uterm.bridge.models import WorkerTermState

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.bridge.hub.core import TermHub
    from provide.uterm.bridge.identity import Principal

logger = get_logger(__name__)


class StateStore:
    """Worker-state heartbeat + lifecycle/policy helper service.

    Composed into :class:`TermHub` as ``self.state``. Holds a back
    reference to the hub for the small set of cross-cutting attributes
    that legitimately live on the hub (the shared ``asyncio.Lock``, the
    worker registry, the per-WS input-buffer dict, the configured
    callbacks).

    Args:
        hub: The composing :class:`TermHub`. The store uses
            ``hub._lock``, ``hub.registry``, ``hub._input_buffers``,
            ``hub._background_tasks``, ``hub._on_metric``,
            ``hub._on_hijack_changed``, ``hub._resolve_browser_role``,
            ``hub._identity_provider`` and ``hub._delegate_roles``.
    """

    __slots__ = ("_hub",)

    def __init__(self, hub: TermHub) -> None:
        self._hub = hub

    # -- Per-browser line buffer ----------------------------------------

    def buffer_and_get_command(self, ws: WebSocket, data: str) -> str | None:
        """Accumulate input for *ws* and return the command if a newline is received."""
        hub = self._hub
        buf = hub._input_buffers.get(ws, "") + data
        if "\r" in buf or "\n" in buf:
            hub._input_buffers.pop(ws, None)
            return buf
        hub._input_buffers[ws] = buf
        return None

    # -- Background-task shutdown ---------------------------------------

    async def shutdown(self) -> None:
        """Cancel all background tasks for graceful shutdown."""
        from provide.uterm.bridge.hub.connections import shutdown_background_tasks

        count = await shutdown_background_tasks(self._hub._background_tasks)
        if count:
            logger.info("hub_shutdown cancelled %d background tasks", count)

    # -- Worker-state heartbeat -----------------------------------------

    async def touch_activity(self, worker_id: str) -> None:
        """Update the last-activity timestamp for *worker_id*."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is not None:  # pragma: no branch
                st.last_activity_at = time.monotonic()

    async def get_or_create(self, worker_id: str) -> WorkerTermState:
        """Return the existing :class:`WorkerTermState` for *worker_id* or create one."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                st = WorkerTermState()
                hub.registry._workers[worker_id] = st
            return st

    # -- Metric callback fan-out ----------------------------------------

    def metric(self, name: str, value: int = 1) -> None:
        """Emit a named metric via the configured on_metric callback."""
        callback = self._hub._on_metric
        if callback is None:
            return
        try:
            callback(name, int(value))
        except Exception as exc:  # pragma: no cover - defensive only
            logger.warning("metric_callback_failed metric=%s error=%s", name, exc)

    # -- Hijack-state predicates ----------------------------------------

    @staticmethod
    def clamp_lease(lease_s: int) -> int:
        """Clamp a lease duration to [1, 14400] seconds (4 hours).

        Matched to the WS idle-reader timeout so a long-running
        operator hold doesn't get killed by either the lease expiry
        or the WS idle reaper. Earlier 3600s cap was too tight for
        multi-hour compare runs that drive a primary target heavily
        while idling extra targets between phases.
        """
        return max(1, min(int(lease_s), 14400))

    @staticmethod
    def has_valid_rest_lease(st: WorkerTermState) -> bool:
        """Return True if *st* has an unexpired REST hijack session."""
        hs = st.hijack_session
        return hs is not None and hs.lease_expires_at > time.monotonic()

    @staticmethod
    def is_dashboard_hijack_active(st: WorkerTermState) -> bool:
        """Return True if a dashboard WS hijack owner exists and its lease has not expired."""
        if st.hijack_owner is None:
            return False
        if st.hijack_owner_expires_at is None:
            return True
        return st.hijack_owner_expires_at > time.monotonic()

    def is_hijacked(self, st: WorkerTermState) -> bool:
        """Return True if *st* is under any active hijack (dashboard WS or REST)."""
        return self.is_dashboard_hijack_active(st) or self.has_valid_rest_lease(st)

    # -- Hijack-changed callback ----------------------------------------

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        """Fire the on_hijack_changed callback (sync or async) without blocking."""
        cb = self._hub._on_hijack_changed
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

    # -- Browser role resolution + policy context -----------------------

    async def resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str:
        """Resolve a browser role via the configured callback; defaults to "viewer"."""
        from fastapi import WebSocketException

        from provide.uterm.bridge.hub.core import BrowserRoleResolutionError

        hub = self._hub
        role = "viewer"
        resolver = hub._resolve_browser_role
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
            raise
        except Exception as exc:
            logger.warning("resolve_browser_role_failed worker_id=%s error=%s", worker_id, exc)
            raise BrowserRoleResolutionError(worker_id) from exc
        if isinstance(resolved_role, str) and resolved_role in {"viewer", "operator", "admin"}:
            return resolved_role
        if resolved_role is not None:
            logger.warning("resolve_browser_role_invalid worker_id=%s role=%r", worker_id, resolved_role)
        return role

    async def prepare_policy_context(self, ws: WebSocket, worker_id: str, action: str | None = None) -> PolicyContext:
        """Create a :class:`PolicyContext` for the given browser WebSocket and worker."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            role = st.browsers.get(ws) if st else None

        principal = None
        if hub._identity_provider:
            principal = await hub._identity_provider.resolve_principal(ws)
        else:
            principal = getattr(getattr(ws, "state", None), "uterm_principal", None)

        if principal and not isinstance(principal, str):
            roles = self._map_roles(principal)
            if roles:
                if "admin" in roles:
                    role = "admin"
                elif "operator" in roles:
                    role = "operator"
                else:
                    role = "viewer"

        client_id = "anonymous"
        if principal:
            client_id = str(principal.subject_id) if hasattr(principal, "subject_id") else str(principal)

        metadata = {"principal": principal} if principal else {}
        return PolicyContext(
            worker_id=worker_id,
            client_id=client_id,
            role=role,
            action=action,
            metadata=metadata,
        )

    def _map_roles(self, principal: Principal) -> frozenset[str]:
        if self._hub._delegate_roles:
            roles = getattr(principal, "roles", None)
            if roles:
                return frozenset(roles)
            return frozenset({"viewer"})

        mapped_roles = set()
        claims = principal.claims or {}
        if claims.get("admin") or claims.get("is_admin"):
            mapped_roles.add("admin")
        elif claims.get("operator"):
            mapped_roles.add("operator")

        if not mapped_roles:
            mapped_roles.add("viewer")
        return frozenset(mapped_roles)


__all__ = ["StateStore"]
