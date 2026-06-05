#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Orchestration-method bodies extracted from :class:`TermHub`.

These functions hold the larger orchestration bodies that used to live
inline on :class:`TermHub` — approval resolution, telemetry emission,
router construction and the worker-hello-mode wrapper. Each
:class:`TermHub` method keeps a thin one-line wrapper (preserving the
no-mixin ``hub.<name>(...)`` call surface) that forwards here with
``hub`` as the first argument. Calls back into the hub go through
``hub.<method>`` so existing tests that monkey-patch those names still
intercept.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger
from provide.uterm.control_channel import encode_data
from provide.uterm.server.bridge.hub.core_helpers import _encode_browser_frame
from provide.uterm.server.bridge.hub.ext import TelemetryEvent

if TYPE_CHECKING:
    from fastapi import APIRouter, WebSocket

    from provide.uterm.server.bridge.hub.core_impl import TermHub
    from provide.uterm.server.bridge.hub.ext import PolicyDecision

logger = get_logger(__name__)


def create_router(hub: TermHub, *, extra_route_registrars: list[Any] | None = None) -> APIRouter:
    """Create and return a FastAPI ``APIRouter`` with all terminal routes registered."""
    from fastapi import APIRouter

    from provide.uterm.server.bridge.routes.rest import register_rest_routes
    from provide.uterm.server.bridge.routes.websockets import register_ws_routes

    router = APIRouter()
    register_rest_routes(hub, router)
    register_ws_routes(hub, router)
    for registrar in extra_route_registrars or []:
        registrar(hub, router)
    return router


async def set_worker_hello_mode(hub: TermHub, worker_id: str, mode: str) -> bool:
    """Backward-compatible wrapper for worker hello mode handling."""
    # Narrow the str arg to InputMode at the wrapper boundary; reject
    # unknown values so the cast on the next line is sound.
    if mode not in ("hijack", "open"):
        raise ValueError(f"invalid input mode: {mode!r}")
    return await hub.set_worker_hello(worker_id, mode)  # type: ignore[arg-type]


async def emit_telemetry(
    hub: TermHub,
    event_type: str,
    *,
    worker_id: str,
    principal: str | None = None,
    role: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit a lifecycle telemetry event to the configured sink.

    Strictly additive and fail-open: if no sink is configured or the
    sink raises, this method silently returns. It must never alter
    control flow, block terminal I/O, or propagate exceptions.
    """
    if hub._telemetry_sink is None:
        return
    evt = TelemetryEvent(
        event_type=event_type,
        worker_id=worker_id,
        principal=principal,
        role=role,
        metadata=metadata or {},
        timestamp=time.time(),
    )
    try:
        await hub._telemetry_sink.emit(evt)
    except Exception:
        # Defensive: WebhookTelemetrySink is already fail-open, but any
        # other sink implementation might raise; absorb it here so
        # emit_telemetry is unconditionally safe to call from lifecycle hooks.
        pass


async def resolve_approval(
    hub: TermHub,
    worker_id: str,
    request_id: str,
    decision: PolicyDecision,
    command: str,
) -> None:
    """Resolve a pending approval and resume the worker if approved."""
    req = hub.approval_store.get(request_id)
    if req and getattr(req, "is_fanout", False):
        if decision.action == "allow":
            fo_ctrl = getattr(hub, "fan_out_controller", None)
            if fo_ctrl:
                await fo_ctrl.release_approved_command(request_id)
        elif decision.action == "deny":
            logger.info(
                "fanout_approval_rejected request_id=%s group_id=%s",
                request_id,
                getattr(req, "group_id", "unknown"),
            )
            fo_ctrl = getattr(hub, "fan_out_controller", None)
            if fo_ctrl:
                await asyncio.to_thread(fo_ctrl._on_approval_expired, request_id)
        return

    st = await hub._get(worker_id)

    # Browsers can disconnect at any await point here. A bare send that raises
    # would abort the whole resolution, leaving other browsers without their
    # approval_resolved frame and stranding them in _paused_browsers. Collect
    # dead sockets and prune them once at the end, mirroring broadcast().
    dead: set[WebSocket] = set()

    if decision.action == "allow":
        await hub.send_worker(worker_id, {"type": "input", "data": command, "ts": time.time()})
    elif decision.action == "deny":
        msg = f"\\r\\x1b[31m[REJECTED] Command '{command.strip()}' blocked by Admin.\\x1b[0m"
        if decision.reason:
            msg += f" \\x1b[33mReason: {decision.reason}\\x1b[0m"
        msg += "\\r"
        for ws in list(st.browsers.keys()):
            try:
                await ws.send_text(encode_data(msg))
            except Exception as exc:
                logger.debug("approval_deny_send_failed worker_id=%s: %s", worker_id, exc)
                dead.add(ws)

    for ws in list(st.browsers.keys()):
        if ws in hub._paused_browsers:
            hub._paused_browsers.discard(ws)
            if decision.action == "allow" and ws in hub._hold_buffers:
                buffered_data = hub._hold_buffers.pop(ws)
                if hub._on_browser_message:  # pragma: no branch — _on_browser_message is wired by app factory; no-handler case is a unit-test artifact

                    async def playback(
                        hub: TermHub,
                        browser_ws: WebSocket,
                        current_worker_id: str,
                        role: str,
                        msg: dict[str, str],
                        owned_hijack: bool,
                    ) -> None:
                        if (
                            hub._on_browser_message
                        ):  # pragma: no branch — entered only when set; recheck inside closure is defensive
                            await hub._on_browser_message(hub, browser_ws, current_worker_id, role, msg, owned_hijack)

                    task = asyncio.create_task(
                        playback(
                            hub,
                            ws,
                            worker_id,
                            st.browsers.get(ws, "viewer"),
                            {"type": "input", "data": buffered_data},
                            False,
                        )
                    )
                    hub._background_tasks.add(task)
                    task.add_done_callback(hub._background_tasks.discard)

        try:
            await ws.send_text(
                _encode_browser_frame(
                    {
                        "type": "approval_resolved",
                        "outcome": "approved" if decision.action == "allow" else "rejected",
                        "request_id": request_id,
                    }
                )
            )
        except Exception as exc:
            logger.debug("approval_resolved_send_failed worker_id=%s: %s", worker_id, exc)
            dead.add(ws)

    if dead:
        await hub.remove_dead_browsers(worker_id, dead)


__all__ = [
    "create_router",
    "emit_telemetry",
    "resolve_approval",
    "set_worker_hello_mode",
]
