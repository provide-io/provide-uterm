#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from provide.telemetry import event


@dataclass(frozen=True)
class PolicyContext:
    """Context for policy decisions in TermHub."""

    worker_id: str
    client_id: str | None = None
    role: str | None = None
    action: str | None = None
    metadata: dict[str, Any] | None = None


@runtime_checkable
class PolicyGate(Protocol):
    """Protocol for external policy enforcement."""

    async def intercept_input(self, data: str, context: PolicyContext) -> bool:
        """Return True if the input is allowed by policy."""
        ...


class NoOpPolicyGate:
    """Default policy gate that allows everything."""

    async def intercept_input(self, _data: str, _context: PolicyContext) -> bool:
        return True


class WebhookPolicyGate:
    """Policy gate that delegates decisions to an external webhook."""

    def __init__(self, url: str, secret: str | None = None, timeout_s: float = 2.0):
        self.url = url
        self.secret = secret
        self.timeout = timeout_s

    async def intercept_input(self, data: str, context: PolicyContext) -> bool:
        payload = {
            "worker_id": context.worker_id,
            "client_id": context.client_id,
            "role": context.role,
            "action": context.action,
            "data": data,
            "metadata": context.metadata,
        }
        # In a real implementation we would add HMAC signatures here if secret is set.
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, json=payload)
                if resp.status_code == 200:
                    return bool(resp.json().get("allow", False))
                return False
        except Exception:
            return False


# Standardized DAS Events for Terminal Sessions
EVENT_SESSION_REGISTERED = event("terminal", "session", "registered")
EVENT_SESSION_DISCONNECTED = event("terminal", "session", "disconnected")
EVENT_HIJACK_ACQUIRED = event("terminal", "hijack", "acquired")
EVENT_HIJACK_RELEASED = event("terminal", "hijack", "released")
EVENT_HIJACK_EXPIRED = event("terminal", "hijack", "expired")
EVENT_RATE_LIMIT_TRIGGERED = event("terminal", "ratelimit", "triggered")
EVENT_RESUME_FAILED = event("terminal", "resume", "failed")
