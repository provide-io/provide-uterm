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
from provide.uterm.control_channel import encode_terminal_data
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
    return await hub.set_worker_hello(worker_id, mode)  # type: ignore[arg-type]  # ty:ignore[invalid-argument-type]


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
    *,
    approval_request: Any | None = None,
) -> tuple[bool, str | None]:
    """Resolve a pending approval and resume the worker if approved."""
    req = approval_request if approval_request is not None else hub.approval_store.get(request_id)
    if req is None:
        return False, "approval_not_found"
    if req.id != request_id or req.worker_id != worker_id:
        return False, "approval_mismatch"

    # The immutable store/claim snapshot is authoritative.  Never inject a
    # caller-supplied command that differs from the request that was approved.
    command = req.command
    if getattr(req, "is_fanout", False):
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
        return True, None

    st = await hub._get(worker_id)

    # Browsers can disconnect at any await point here. A bare send that raises
    # would abort the whole resolution, leaving other browsers without their
    # approval_resolved frame and stranding them in _paused_browsers. Collect
    # dead sockets and prune them once at the end, mirroring broadcast().
    dead: set[WebSocket] = set()

    delivered = True
    refusal_reason: str | None = None
    replay_pending = False
    if decision.action == "allow":
        origin_browser = getattr(req, "origin_browser", None)
        ownership_generation = getattr(req, "ownership_generation", None)
        if origin_browser is None or ownership_generation is None:
            delivered, refusal_reason = False, "invalid_owner"
        else:
            from provide.uterm.server.bridge.routes.browser_handlers import _handle_input

            async def deliver_and_replay(send_reserved: Any) -> tuple[bool, str | None]:
                command_sent = await send_reserved({"type": "input", "data": command, "ts": time.time()})
                if not command_sent:
                    return False, "no_worker"

                replay_result: str | None = None
                while buffered_data := hub._hold_buffers.pop(origin_browser, None):
                    outcome = await _handle_input(
                        hub,
                        origin_browser,
                        worker_id,
                        {"type": "input", "data": buffered_data},
                        bypass_pause=True,
                        ownership_generation_override=ownership_generation,
                        reserved_sender=send_reserved,
                    )
                    if outcome == "held":
                        return True, "replay_pending"
                    if outcome in {"blocked", "collision"}:
                        replay_result = "replay_blocked"
                    elif outcome == "send_failed":
                        return True, "replay_failed"
                return True, replay_result

            operation_result, owner_error = await hub.run_owned_browser_operation(
                worker_id,
                deliver_and_replay,
                browser_ws=origin_browser,
                ownership_generation=ownership_generation,
                source=origin_browser,
            )
            if operation_result is None:
                delivered, refusal_reason = False, owner_error
            else:
                delivered, refusal_reason = operation_result
                replay_pending = refusal_reason == "replay_pending"
    elif decision.action == "deny":
        msg = f"\\r\\x1b[31m[REJECTED] Command '{command.strip()}' blocked by Admin.\\x1b[0m"
        if decision.reason:
            msg += f" \\x1b[33mReason: {decision.reason}\\x1b[0m"
        msg += "\\r"
        for ws in list(st.browsers.keys()):
            try:
                await ws.send_text(encode_terminal_data(msg))
            except Exception as exc:
                logger.debug("approval_deny_send_failed worker_id=%s: %s", worker_id, exc)
                dead.add(ws)

    for ws in list(st.browsers.keys()):
        is_origin = getattr(req, "origin_browser", None) is None or ws is req.origin_browser
        if ws in hub._paused_browsers and is_origin:
            if not replay_pending:
                hub._paused_browsers.discard(ws)
            if decision.action != "allow" or not delivered:
                hub._hold_buffers.pop(ws, None)

        try:
            await ws.send_text(
                _encode_browser_frame(
                    {
                        "type": "approval_resolved",
                        "outcome": (
                            "approved"
                            if decision.action == "allow" and delivered
                            else "refused"
                            if decision.action == "allow"
                            else "rejected"
                        ),
                        "request_id": request_id,
                        "detail": refusal_reason,
                    }
                )
            )
        except Exception as exc:
            logger.debug("approval_resolved_send_failed worker_id=%s: %s", worker_id, exc)
            dead.add(ws)

    if dead:
        await hub.remove_dead_browsers(worker_id, dead)
    await hub.append_event(
        worker_id,
        "approval_resolved",
        {
            "request_id": request_id,
            "outcome": (
                "approved"
                if decision.action == "allow" and delivered
                else "refused"
                if decision.action == "allow"
                else "rejected"
            ),
            "detail": refusal_reason,
        },
    )
    return delivered, refusal_reason


__all__ = [
    "create_router",
    "emit_telemetry",
    "resolve_approval",
    "set_worker_hello_mode",
]
