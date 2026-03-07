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

from provide.terminal.hijack.hub.ownership import _HijackOwnershipMixin
from provide.terminal.hijack.models import WorkerTermState, extract_prompt_id

logger = logging.getLogger(__name__)

# Callback: (worker_id, is_hijacked, owner_or_None)
HijackStateCallback = Callable[[str, bool, str | None], Awaitable[None] | None]
BrowserRoleResolver = Callable[[WebSocket, str], str | None | Awaitable[str | None]]


class BrowserRoleResolutionError(RuntimeError):
    """Raised when a browser-role resolver fails and the WS should be rejected."""


class TermHub(_HijackOwnershipMixin):
    """In-memory registry for terminal WebSocket connections.

    Manages the lifecycle of worker / browser terminal streams and hijack leases.

    Args:
        on_hijack_changed: Optional async or sync callback invoked whenever hijack
            state changes for any worker. Signature: ``(worker_id, hijacked, owner) -> None``.
        dashboard_hijack_lease_s: Default dashboard WS hijack lease duration in seconds.
    """

    def __init__(
        self,
        on_hijack_changed: HijackStateCallback | None = None,
        dashboard_hijack_lease_s: int = 45,
        *,
        resolve_browser_role: BrowserRoleResolver | None = None,
        max_ws_message_bytes: int = 1_048_576,
        max_input_chars: int = 10_000,
        browser_rate_limit_per_sec: float = 30,
    ) -> None:
        self._lock = asyncio.Lock()
        self._workers: dict[str, WorkerTermState] = {}
        self._on_hijack_changed = on_hijack_changed
        self._resolve_browser_role = resolve_browser_role
        self._dashboard_hijack_lease_s = max(1, min(int(dashboard_hijack_lease_s), 600))
        self.max_ws_message_bytes = max(1024, int(max_ws_message_bytes))
        self.max_input_chars = max(100, int(max_input_chars))
        self.browser_rate_limit_per_sec = float(browser_rate_limit_per_sec)

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
            task: asyncio.Task[object] = asyncio.create_task(result)  # type: ignore[arg-type]
            task.add_done_callback(
                lambda t: (
                    logger.warning("on_hijack_changed callback raised worker_id=%s error=%s", worker_id, t.exception())
                    if not t.cancelled() and t.exception() is not None
                    else None
                )
            )

    async def _resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str:
        role = "viewer"
        resolver = self._resolve_browser_role
        if resolver is None:
            return role
        try:
            resolved_role = resolver(ws, worker_id)
            if inspect.isawaitable(resolved_role):
                resolved_role = await resolved_role
        except Exception as exc:
            logger.warning("resolve_browser_role_failed worker_id=%s error=%s", worker_id, exc)
            raise BrowserRoleResolutionError(worker_id) from exc
        if isinstance(resolved_role, str) and resolved_role in {"viewer", "operator", "admin"}:
            return resolved_role
        if resolved_role is not None:
            logger.warning("resolve_browser_role_invalid worker_id=%s role=%r", worker_id, resolved_role)
        return role

    async def _append_event(
        self, worker_id: str, event_type: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = data or {}
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return {"seq": 0, "ts": time.time(), "type": event_type, "data": payload}
            st.event_seq += 1
            evt: dict[str, Any] = {"seq": st.event_seq, "ts": time.time(), "type": event_type, "data": payload}
            st.events.append(evt)
            return evt

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
        return None

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
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return
            browsers = list(st.browsers.keys())
        dead: set[WebSocket] = set()
        payload = json.dumps(msg, ensure_ascii=True)
        for ws in browsers:
            try:
                await ws.send_text(payload)
            except Exception as exc:
                logger.debug("broadcast_send_failed worker_id=%s: %s", worker_id, exc)
                dead.add(ws)
        if dead:
            notify_hijack_off = False
            async with self._lock:
                st2 = self._workers.get(worker_id)
                if st2 is not None:
                    for ws in dead:
                        st2.browsers.pop(ws, None)
                        if self._is_dashboard_hijack_active(st2) and st2.hijack_owner is ws:
                            st2.hijack_owner = None
                            st2.hijack_owner_expires_at = None
                            notify_hijack_off = not self._is_rest_session_active(st2)
            if notify_hijack_off:
                async with self._lock:
                    _st3 = self._workers.get(worker_id)
                    if _st3 is not None and self._is_hijacked(_st3):
                        notify_hijack_off = False
            if notify_hijack_off:
                await self._send_worker(
                    worker_id,
                    {"type": "control", "action": "resume", "owner": "dead-socket", "lease_s": 0, "ts": time.time()},
                )
                self._notify_hijack_changed(worker_id, enabled=False, owner=None)
            await self._broadcast_hijack_state(worker_id)

    async def _broadcast_hijack_state(self, worker_id: str) -> None:
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return
            browsers = list(st.browsers.keys())
            hijack_owner = st.hijack_owner
            is_hijacked = self._is_hijacked(st)
            is_dashboard = self._is_dashboard_hijack_active(st)
            is_rest = self._is_rest_session_active(st)
            input_mode = st.input_mode
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
                            "input_mode": input_mode,
                        },
                        ensure_ascii=True,
                    )
                )
            except Exception as exc:
                logger.debug("broadcast_hijack_state_send_failed worker_id=%s: %s", worker_id, exc)
                dead.add(ws)
        if dead:
            notify_hijack_off = False
            async with self._lock:
                st2 = self._workers.get(worker_id)
                if st2 is not None:
                    for ws in dead:
                        st2.browsers.pop(ws, None)
                        if self._is_dashboard_hijack_active(st2) and st2.hijack_owner is ws:
                            st2.hijack_owner = None
                            st2.hijack_owner_expires_at = None
                            notify_hijack_off = not self._is_rest_session_active(st2)
            if notify_hijack_off:
                async with self._lock:
                    _st3 = self._workers.get(worker_id)
                    if _st3 is not None and self._is_hijacked(_st3):
                        notify_hijack_off = False
            if notify_hijack_off:
                await self._send_worker(
                    worker_id,
                    {"type": "control", "action": "resume", "owner": "dead-socket", "lease_s": 0, "ts": time.time()},
                )
                self._notify_hijack_changed(worker_id, enabled=False, owner=None)
            await self._broadcast_hijack_state(worker_id)

    async def _send_worker(self, worker_id: str, msg: dict[str, Any]) -> bool:
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
            if st.worker_ws is None and not st.browsers and st.hijack_owner is None and st.hijack_session is None:
                del self._workers[worker_id]
                logger.debug("pruned idle worker_id=%s", worker_id)

    async def _hijack_state_msg_for(self, worker_id: str, ws: WebSocket) -> dict[str, Any]:
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return {
                    "type": "hijack_state",
                    "hijacked": False,
                    "owner": None,
                    "lease_expires_at": None,
                    "input_mode": "hijack",
                }
            is_dashboard = self._is_dashboard_hijack_active(st)
            is_rest = self._is_rest_session_active(st)
            is_h = is_dashboard or is_rest
            input_mode = st.input_mode
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
            "input_mode": input_mode,
        }

    async def _set_input_mode(self, worker_id: str, mode: str) -> tuple[bool, str | None]:
        """Set input_mode under lock. Rejects if active hijack when switching to "open".

        Returns:
            ``(True, None)`` on success.
            ``(False, "not_found")`` if worker not registered.
            ``(False, "active_hijack")`` if a hijack is active and mode is "open".
        """
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return False, "not_found"
            if mode == "open" and self._is_hijacked(st):
                return False, "active_hijack"
            st.input_mode = mode
        await self._broadcast(
            worker_id,
            {"type": "input_mode_changed", "input_mode": mode, "ts": time.time()},
        )
        await self._broadcast_hijack_state(worker_id)
        return True, None

    async def _disconnect_worker(self, worker_id: str) -> bool:
        """Programmatically disconnect the worker WS. Returns True if a worker was connected."""
        ws: WebSocket | None = None
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or st.worker_ws is None:
                return False
            ws = st.worker_ws
            st.worker_ws = None
            # Clear hijack state atomically
            was_hijacked = st.hijack_session is not None or st.hijack_owner is not None
            st.hijack_session = None
            st.hijack_owner = None
            st.hijack_owner_expires_at = None
        # Close WS outside lock
        try:
            await ws.close()
        except Exception as exc:
            logger.debug("disconnect_worker close error worker_id=%s: %s", worker_id, exc)
        await self._broadcast(
            worker_id,
            {"type": "worker_disconnected", "worker_id": worker_id, "ts": time.time()},
        )
        if was_hijacked:
            self._notify_hijack_changed(worker_id, enabled=False, owner=None)
            await self._broadcast_hijack_state(worker_id)
        await self._prune_if_idle(worker_id)
        return True

    def _can_send_input(self, st: WorkerTermState, ws: WebSocket) -> bool:
        """Check if *ws* can send input to the worker (open mode or hijack owner).

        In open mode, viewers are excluded — only operators and admins may send.
        """
        if st.input_mode == "open":
            role = st.browsers.get(ws, "viewer")
            return role in ("operator", "admin")
        return self._is_dashboard_hijack_active(st) and st.hijack_owner is ws

    async def _request_snapshot(self, worker_id: str) -> None:
        await self._send_worker(worker_id, {"type": "snapshot_req", "req_id": str(uuid.uuid4()), "ts": time.time()})

    async def _request_analysis(self, worker_id: str) -> None:
        await self._send_worker(worker_id, {"type": "analyze_req", "req_id": str(uuid.uuid4()), "ts": time.time()})

    def create_router(self) -> Any:
        """Create and return a FastAPI ``APIRouter`` with all terminal routes registered."""
        from fastapi import APIRouter

        from provide.terminal.hijack.routes.rest import register_rest_routes
        from provide.terminal.hijack.routes.websockets import register_ws_routes

        router = APIRouter()
        register_rest_routes(self, router)
        register_ws_routes(self, router)
        return router
