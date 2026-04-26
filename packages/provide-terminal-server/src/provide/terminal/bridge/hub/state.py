#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
import inspect
import time
from typing import TYPE_CHECKING

from provide.telemetry import get_logger
from provide.terminal.bridge.hub.ext import PolicyContext
from provide.terminal.bridge.models import WorkerTermState

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.terminal.bridge.hub.event_bus import EventBus
    from provide.terminal.bridge.identity import Principal

logger = get_logger(__name__)


class HubStateMixin:
    @property
    def event_bus(self) -> EventBus | None:
        """Public accessor for the EventBus instance (None if not configured)."""
        return self._event_bus

    @event_bus.setter
    def event_bus(self, value: EventBus | None) -> None:
        """Backward-compatible setter used by tests and app wiring."""
        self._event_bus = value

    def _buffer_and_get_command(self, ws: WebSocket, data: str) -> str | None:
        """Accumulate input for *ws* and return the command if a newline is received."""
        buf = self._input_buffers.get(ws, "") + data
        if "\r" in buf or "\n" in buf:
            self._input_buffers.pop(ws, None)
            return buf
        self._input_buffers[ws] = buf
        return None

    async def shutdown(self) -> None:
        """Cancel all background tasks for graceful shutdown."""
        from provide.terminal.bridge.hub.connections import shutdown_background_tasks

        count = await shutdown_background_tasks(self._background_tasks)
        if count:
            logger.info("hub_shutdown cancelled %d background tasks", count)

    async def touch_activity(self, worker_id: str) -> None:
        """Update the last-activity timestamp for *worker_id*."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is not None:  # pragma: no branch
                st.last_activity_at = time.monotonic()

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
        from fastapi import WebSocketException

        from provide.terminal.bridge.hub.core import BrowserRoleResolutionError

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
        """Create a PolicyContext for the given browser WebSocket and worker."""
        async with self._lock:
            st = self._workers.get(worker_id)
            role = st.browsers.get(ws) if st else None

        principal = None
        if self._identity_provider:
            principal = await self._identity_provider.resolve_principal(ws)
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
        if self._delegate_roles:
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
