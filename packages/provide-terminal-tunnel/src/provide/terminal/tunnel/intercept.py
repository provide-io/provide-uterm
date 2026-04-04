#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""InterceptGate — pause/resume state machine for HTTP request interception."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, TypedDict

logger = logging.getLogger(__name__)


class InterceptDecision(TypedDict):
    """Browser's decision for a paused request."""

    action: str  # "forward" | "drop" | "modify"
    headers: dict[str, str] | None
    body: bytes | None


def _default_decision(action: str) -> InterceptDecision:
    return InterceptDecision(action=action, headers=None, body=None)


def parse_action_message(msg: dict[str, Any]) -> InterceptDecision:
    """Parse an http_action message from the browser into an InterceptDecision."""
    action = str(msg.get("action", "forward"))
    if action not in ("forward", "drop", "modify"):
        action = "forward"
    headers: dict[str, str] | None = None
    body: bytes | None = None
    if action == "modify":
        raw_headers = msg.get("headers")
        if isinstance(raw_headers, dict):
            headers = {str(k): str(v) for k, v in raw_headers.items()}
        body_b64 = msg.get("body_b64")
        if isinstance(body_b64, str):
            try:
                body = base64.b64decode(body_b64, validate=True)
            except Exception:
                logger.warning("intercept_invalid_body_b64 id=%s", msg.get("id"))
    return InterceptDecision(action=action, headers=headers, body=body)


class InterceptGate:
    """Manages pending intercepted HTTP requests as asyncio Futures."""

    def __init__(self, timeout_s: float = 30.0, timeout_action: str = "forward") -> None:
        self.enabled: bool = False
        self.inspect_enabled: bool = True
        self.timeout_s: float = max(1.0, timeout_s)
        self.timeout_action: str = timeout_action if timeout_action in ("forward", "drop") else "forward"
        self._pending: dict[str, asyncio.Future[InterceptDecision]] = {}

    @property
    def pending_count(self) -> int:
        """Number of requests currently awaiting a browser decision."""
        return len(self._pending)

    async def await_decision(self, rid: str) -> InterceptDecision:
        """Block until browser sends http_action or timeout expires."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[InterceptDecision] = loop.create_future()
        self._pending[rid] = fut
        try:
            return await asyncio.wait_for(fut, timeout=self.timeout_s)
        except TimeoutError:
            return _default_decision(self.timeout_action)
        finally:
            self._pending.pop(rid, None)

    def resolve(self, rid: str, decision: InterceptDecision) -> bool:
        """Resolve a pending request. Returns True if found and resolved."""
        fut = self._pending.get(rid)
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True

    def cancel_all(self, action: str = "forward") -> int:
        """Resolve all pending requests. Returns count resolved."""
        decision = _default_decision(action)
        count = 0
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(decision)
                count += 1
        self._pending.clear()
        return count
