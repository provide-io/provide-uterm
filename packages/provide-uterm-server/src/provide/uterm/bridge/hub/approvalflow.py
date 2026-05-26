#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, cast

from provide.telemetry import get_logger
from provide.uterm.control_channel import encode_data

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import WebSocket

    from provide.uterm.bridge.hub.approvals import InMemoryApprovalStore
    from provide.uterm.bridge.hub.core import TermHub
    from provide.uterm.bridge.hub.ext import PolicyDecision

logger = get_logger(__name__)


class HubApprovalFlowMixin:
    """Buffered approval flow for hijack handoffs.

    Composed into :class:`provide.uterm.bridge.hub.core.TermHub`. The
    underscore-prefixed attributes below are *type-only* declarations
    that describe what the composing class must initialise; see the
    same pattern on ``HubMessagingMixin`` and ``HubStateMixin``.

    Approval-request storage lives on the composing hub as
    :attr:`TermHub.approval_store` (an :class:`InMemoryApprovalStore`);
    this mixin holds the orchestration policy — worker resume, browser
    rejection notice, paused-browser playback, and approval-resolved
    control-frame fanout — that surrounds the store's CRUD surface.
    """

    approval_store: InMemoryApprovalStore
    _background_tasks: set[Any]
    _hold_buffers: dict[Any, str]
    _paused_browsers: set[Any]
    _on_browser_message: Any | None

    if TYPE_CHECKING:
        _get: Callable[..., Any]

        # Mirrors HubMessagingMixin.send_worker exactly (per-mixin type-only
        # stubs must match the canonical signature across the MRO).
        async def send_worker(self, worker_id: str, msg: dict[str, Any], *, source: Any = None) -> bool: ...

    async def resolve_approval(self, worker_id: str, request_id: str, decision: PolicyDecision, command: str) -> None:
        """Resolve a pending approval and resume the worker if approved."""
        from provide.uterm.bridge.hub.core import _encode_browser_frame

        req = self.approval_store.get(request_id)
        if req and getattr(req, "is_fanout", False):
            if decision.action == "allow":
                fo_ctrl = getattr(self, "fan_out_controller", None)
                if fo_ctrl:
                    await fo_ctrl.release_approved_command(request_id)
            elif decision.action == "deny":
                logger.info(
                    "fanout_approval_rejected request_id=%s group_id=%s",
                    request_id,
                    getattr(req, "group_id", "unknown"),
                )
                fo_ctrl = getattr(self, "fan_out_controller", None)
                if fo_ctrl:
                    await asyncio.to_thread(fo_ctrl._on_approval_expired, request_id)
            return

        st = await self._get(worker_id)

        if decision.action == "allow":
            await self.send_worker(worker_id, {"type": "input", "data": command, "ts": time.time()})
        elif decision.action == "deny":
            msg = f"\\r\\x1b[31m[REJECTED] Command '{command.strip()}' blocked by Admin.\\x1b[0m"
            if decision.reason:
                msg += f" \\x1b[33mReason: {decision.reason}\\x1b[0m"
            msg += "\\r"
            for ws in list(st.browsers.keys()):
                await ws.send_text(encode_data(msg))

        for ws in list(st.browsers.keys()):
            if ws in self._paused_browsers:
                self._paused_browsers.discard(ws)
                if decision.action == "allow" and ws in self._hold_buffers:
                    buffered_data = self._hold_buffers.pop(ws)
                    if self._on_browser_message:  # pragma: no branch — _on_browser_message is wired by app factory; no-handler case is a unit-test artifact

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
                                await hub._on_browser_message(
                                    hub, browser_ws, current_worker_id, role, msg, owned_hijack
                                )

                        task = asyncio.create_task(
                            playback(
                                cast("TermHub", self),
                                ws,
                                worker_id,
                                st.browsers.get(ws, "viewer"),
                                {"type": "input", "data": buffered_data},
                                False,
                            )
                        )
                        self._background_tasks.add(task)
                        task.add_done_callback(self._background_tasks.discard)

            await ws.send_text(
                _encode_browser_frame(
                    {
                        "type": "approval_resolved",
                        "outcome": "approved" if decision.action == "allow" else "rejected",
                        "request_id": request_id,
                    }
                )
            )
