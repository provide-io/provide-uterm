#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, Field

from provide.telemetry import event


@dataclass(frozen=True)
class PolicyContext:
    """Context for policy decisions in TermHub."""

    worker_id: str
    client_id: str | None = None
    role: str | None = None
    action: str | None = None
    metadata: dict[str, Any] | None = None


class PolicyDecision(BaseModel):
    """Decision returned by a PolicyGate."""

    action: str = Field(..., pattern="^(allow|deny|hold)$")
    request_id: str | None = None
    timeout_s: int = 60
    reason: str | None = None


@runtime_checkable
class PolicyGate(Protocol):
    """Protocol for external policy enforcement."""

    async def intercept_input(self, data: str, context: PolicyContext) -> PolicyDecision:
        """Return a PolicyDecision for the input."""
        ...


class NoOpPolicyGate:
    """Default policy gate that allows everything."""

    async def intercept_input(self, _data: str, _context: PolicyContext) -> PolicyDecision:
        return PolicyDecision(action="allow")


class WebhookPolicyGate:
    """Policy gate that delegates decisions to an external webhook."""

    def __init__(self, url: str, secret: str | None = None, timeout_s: float = 2.0):
        self.url = url
        self.secret = secret
        self.timeout = timeout_s

    async def intercept_input(self, data: str, context: PolicyContext) -> PolicyDecision:
        payload = {
            "worker_id": context.worker_id,
            "client_id": context.client_id,
            "role": context.role,
            "action": context.action,
            "data": data,
            "metadata": context.metadata,
        }
        body = _encode_webhook_payload(payload)
        headers = _build_webhook_headers(self.secret, body)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, content=body, headers=headers)
                if resp.status_code == 200:
                    body = resp.json()
                    if "action" in body:
                        return PolicyDecision(**body)
                    allow = bool(body.get("allow", False))
                    return PolicyDecision(action="allow" if allow else "deny")
                return PolicyDecision(action="deny")
        except Exception:
            return PolicyDecision(action="deny")


@runtime_checkable
class FanOutPolicyGate(Protocol):
    """Protocol for fan-out approval gating."""

    async def intercept_fanout(
        self,
        command: str,
        context: PolicyContext,
        group_id: str | None = None,
    ) -> PolicyDecision:
        """Evaluate if a fan-out command can proceed and return a gating decision."""
        ...


class WebhookFanOutPolicyGate:
    """Fan-out policy gate that delegates to an external webhook."""

    def __init__(self, url: str, secret: str | None = None, timeout_s: float = 2.0):
        self.url = url
        self.secret = secret
        self.timeout = timeout_s

    async def intercept_fanout(
        self,
        command: str,
        context: PolicyContext,
        group_id: str | None = None,
    ) -> PolicyDecision:
        payload = {
            "worker_id": context.worker_id,
            "client_id": context.client_id,
            "role": context.role,
            "command": command,
            "group_id": group_id,
            "metadata": context.metadata,
        }
        body = _encode_webhook_payload(payload)
        headers = _build_webhook_headers(self.secret, body)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, content=body, headers=headers)
                if resp.status_code == 200:
                    return PolicyDecision(**resp.json())
                return PolicyDecision(action="deny")
        except Exception:
            return PolicyDecision(action="deny")


class RedactionRule(BaseModel):
    """A regex-based redaction rule."""

    pattern: str
    replacement: str = "[REDACTED]"


@runtime_checkable
class OutputPolicyGate(Protocol):
    """Protocol for real-time terminal output inspection and redaction rules."""

    async def get_redaction_rules(self, context: PolicyContext) -> list[RedactionRule]:
        """Return the active redaction rules for this context."""
        ...


class ConnectionHeuristics(BaseModel):
    """Behavioral metrics for a single connection."""

    cps: float
    jitter: float
    timestamp: float


class BehavioralThresholds(BaseModel):
    """Thresholds for behavioral anomaly detection."""

    max_cps: float | None = None
    min_jitter: float | None = None


@runtime_checkable
class BehavioralAuditGate(Protocol):
    """Protocol for behavioral anomaly detection and gating."""

    async def audit_connection(
        self,
        heuristics: ConnectionHeuristics,
        context: PolicyContext,
        thresholds: BehavioralThresholds,
    ) -> PolicyDecision:
        """Evaluate behavioral metrics and return a gating decision."""
        ...


class NoOpBehavioralAuditGate:
    """Default behavioral gate that allows everything."""

    async def audit_connection(
        self,
        _heuristics: ConnectionHeuristics,
        _context: PolicyContext,
        _thresholds: BehavioralThresholds,
    ) -> PolicyDecision:
        return PolicyDecision(action="allow")


class WebhookBehavioralAuditGate:
    """Behavioral gate that delegates to an external webhook."""

    def __init__(self, url: str, secret: str | None = None, timeout_s: float = 2.0, *, fail_open: bool = False):
        # fail_open: when the webhook errors, allow (True) instead of the
        # secure default deny (False). Programmatic opt-out; surfacing it as a
        # GovernanceConfig field is a planned follow-up.
        self.url = url
        self.secret = secret
        self.timeout = timeout_s
        self.fail_open = fail_open

    async def audit_connection(
        self,
        heuristics: ConnectionHeuristics,
        context: PolicyContext,
        thresholds: BehavioralThresholds,
    ) -> PolicyDecision:
        payload = {
            "worker_id": context.worker_id,
            "client_id": context.client_id,
            "heuristics": heuristics.model_dump(),
            "thresholds": thresholds.model_dump(),
            "metadata": context.metadata,
        }
        body = _encode_webhook_payload(payload)
        headers = _build_webhook_headers(self.secret, body)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, content=body, headers=headers)
                if resp.status_code == 200:
                    return PolicyDecision(**resp.json())
                return PolicyDecision(action="allow" if self.fail_open else "deny")
        except Exception:
            return PolicyDecision(action="allow" if self.fail_open else "deny")


# Standardized DAS Events for Terminal Sessions
EVENT_SESSION_REGISTERED = event("terminal", "session", "registered")
EVENT_SESSION_DISCONNECTED = event("terminal", "session", "disconnected")
EVENT_HIJACK_ACQUIRED = event("terminal", "hijack", "acquired")
EVENT_HIJACK_RELEASED = event("terminal", "hijack", "released")
EVENT_HIJACK_EXPIRED = event("terminal", "hijack", "expired")
EVENT_RATE_LIMIT_TRIGGERED = event("terminal", "ratelimit", "triggered")
EVENT_RESUME_FAILED = event("terminal", "resume", "failed")


class NoOpOutputPolicyGate:
    """Default output policy gate that performs no redaction."""

    async def get_redaction_rules(self, _context: PolicyContext) -> list[RedactionRule]:
        return []


class WebhookOutputPolicyGate:
    """Output policy gate that fetches redaction patterns from an external webhook."""

    def __init__(self, url: str, secret: str | None = None, timeout_s: float = 2.0):
        self.url = url
        self.secret = secret
        self.timeout = timeout_s

    async def get_redaction_rules(self, context: PolicyContext) -> list[RedactionRule]:
        payload = {
            "worker_id": context.worker_id,
            "client_id": context.client_id,
            "role": context.role,
            "action": "get_redaction_rules",
            "metadata": context.metadata,
        }
        body = _encode_webhook_payload(payload)
        headers = _build_webhook_headers(self.secret, body)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, content=body, headers=headers)
                if resp.status_code == 200:
                    rules_data = resp.json().get("rules", [])
                    return [RedactionRule(**r) for r in rules_data]
                return []
        except Exception:
            return []


def _encode_webhook_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _build_webhook_headers(secret: str | None, body: bytes) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if secret:
        headers["X-Webhook-Secret"] = secret
        sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Uterm-Signature"] = f"sha256={sig}"
    return headers
