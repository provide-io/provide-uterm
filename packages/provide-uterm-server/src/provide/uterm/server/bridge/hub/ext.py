#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, Field

from provide.telemetry import event, get_logger
from provide.uterm.server.tracing import inject_trace_context
from provide.uterm.server.webhook_signing import build_webhook_signature

_sink_logger = get_logger(__name__)


async def _assert_webhook_target_allowed(url: str) -> None:
    """Lazy wrapper around the egress SSRF guard.

    Imported lazily to break the ``egress -> webhooks -> bridge.hub -> egress``
    import cycle: ``egress`` must be importable cold (e.g. via
    ``pam_integration``) without this module holding a module-level back-
    reference while ``egress`` is still initialising.
    """
    from provide.uterm.server.egress import assert_webhook_target_allowed

    await assert_webhook_target_allowed(url)


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
        # Reuse one client across calls so HTTP keep-alive / connection pooling
        # avoids a fresh TLS handshake per keystroke (intercept_input is hot).
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        """Release the pooled client's connection pool (lifecycle cleanup)."""
        await self._client.aclose()

    async def intercept_input(self, data: str, context: PolicyContext) -> PolicyDecision:
        # Redact secrets out of the keystroke stream before it leaves the
        # process — the governance webhook is an external endpoint and must
        # never receive passwords/keys verbatim.
        payload = {
            "worker_id": context.worker_id,
            "client_id": context.client_id,
            "role": context.role,
            "action": context.action,
            "data": _governance_redactor().redact(data),
            "metadata": context.metadata,
        }
        body = _encode_webhook_payload(payload)
        headers = _build_webhook_headers(self.secret, body)
        try:
            await _assert_webhook_target_allowed(self.url)
            resp = await self._client.post(self.url, content=body, headers=headers)
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
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        """Release the pooled client's connection pool (lifecycle cleanup)."""
        await self._client.aclose()

    async def intercept_fanout(
        self,
        command: str,
        context: PolicyContext,
        group_id: str | None = None,
    ) -> PolicyDecision:
        # Redact secrets out of the forwarded command for the same reason as
        # intercept_input — the fan-out webhook is an external endpoint.
        payload = {
            "worker_id": context.worker_id,
            "client_id": context.client_id,
            "role": context.role,
            "command": _governance_redactor().redact(command),
            "group_id": group_id,
            "metadata": context.metadata,
        }
        body = _encode_webhook_payload(payload)
        headers = _build_webhook_headers(self.secret, body)
        try:
            await _assert_webhook_target_allowed(self.url)
            resp = await self._client.post(self.url, content=body, headers=headers)
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
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        """Release the pooled client's connection pool (lifecycle cleanup)."""
        await self._client.aclose()

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
            await _assert_webhook_target_allowed(self.url)
            resp = await self._client.post(self.url, content=body, headers=headers)
            if resp.status_code == 200:
                return PolicyDecision(**resp.json())
            return PolicyDecision(action="allow" if self.fail_open else "deny")
        except Exception:
            return PolicyDecision(action="allow" if self.fail_open else "deny")


class TelemetryEvent(BaseModel):
    """Lifecycle telemetry event emitted by the AGPL node to a Fleet Manager sink."""

    event_type: str
    worker_id: str
    principal: str | None = None
    role: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Caller supplies the timestamp via time.time() — models stay pure (no
    # side-effectful defaults) so they can be constructed deterministically
    # in tests without patching time.
    timestamp: float


@runtime_checkable
class TelemetrySink(Protocol):
    """Protocol for the upward Node→Fleet-Manager telemetry channel."""

    async def emit(self, event: TelemetryEvent) -> None:
        """Emit a lifecycle event to the sink. Must never raise."""
        ...


class NoOpTelemetrySink:
    """Default telemetry sink that discards all events."""

    async def emit(self, event: TelemetryEvent) -> None:
        _ = event  # no-op sink; event intentionally discarded
        return


class WebhookTelemetrySink:
    """Telemetry sink that POSTs signed events to an external webhook.

    Telemetry is always **fail-open**: any transport or egress error is
    swallowed and logged at DEBUG level so a misbehaving or unreachable
    Fleet Manager endpoint can never block terminal I/O or alter control flow.
    """

    def __init__(self, url: str, secret: str | None = None, timeout_s: float = 2.0) -> None:
        self.url = url
        self.secret = secret
        self.timeout = timeout_s
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        """Release the pooled client's connection pool (lifecycle cleanup)."""
        await self._client.aclose()

    async def emit(self, event: TelemetryEvent) -> None:
        payload = event.model_dump()
        body = _encode_webhook_payload(payload)
        headers = _build_webhook_headers(self.secret, body)
        try:
            await _assert_webhook_target_allowed(self.url)
            await self._client.post(self.url, content=body, headers=headers)
        except Exception as exc:
            # Fail-open: telemetry must never raise into the hub or block I/O.
            _sink_logger.debug("telemetry_sink_error url=%s: %s", self.url, exc)


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
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        """Release the pooled client's connection pool (lifecycle cleanup)."""
        await self._client.aclose()

    async def get_redaction_rules(self, context: PolicyContext) -> list[RedactionRule]:
        # Imported lazily: redaction_defaults imports RedactionRule from this
        # module, so a top-level import here would be circular.
        from provide.uterm.server.bridge.hub.redaction_defaults import default_rules

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
            await _assert_webhook_target_allowed(self.url)
            resp = await self._client.post(self.url, content=body, headers=headers)
            if resp.status_code == 200:
                rules_data = resp.json().get("rules", [])
                return [RedactionRule(**r) for r in rules_data]
            # Fail CLOSED: a non-200 webhook response must not silently
            # disable redaction. Fall back to the built-in default ruleset
            # so secrets keep getting redacted from terminal output.
            return default_rules()  # ty: ignore[invalid-return-type]  # ty can't resolve default_rules() across the lazy (circular-avoiding) import
        except Exception:
            # Fail CLOSED on any transport/egress error — same rationale.
            return default_rules()  # ty: ignore[invalid-return-type]  # ty can't resolve default_rules() across the lazy (circular-avoiding) import


# Lazily-built singleton redactor for outbound governance webhook payloads.
# Built lazily because redaction_defaults imports RedactionRule from this
# module, so importing it at module load time would be circular. The redactor
# is stateless after construction, so a single shared instance is safe.
_GOVERNANCE_REDACTOR: Any = None


def _governance_redactor() -> Any:
    """Return the shared StreamRedactor seeded with the built-in default rules."""
    global _GOVERNANCE_REDACTOR
    if _GOVERNANCE_REDACTOR is None:
        from provide.uterm.server.bridge.hub.redaction import StreamRedactor
        from provide.uterm.server.bridge.hub.redaction_defaults import default_rules

        _GOVERNANCE_REDACTOR = StreamRedactor(default_rules())
    return _GOVERNANCE_REDACTOR


def _encode_webhook_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _build_webhook_headers(secret: str | None, body: bytes) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if secret:
        ts = str(time.time())
        headers["X-Uterm-Timestamp"] = ts
        headers["X-Uterm-Signature"] = build_webhook_signature(secret, body, ts)
    # Propagate the active W3C trace context (traceparent) onto the outbound
    # governance webhook so distributed traces survive the hop. Via
    # provide.telemetry (OpenTelemetry-optional) — no-op when no span is active.
    # Covers all four gate classes, which share this builder.
    inject_trace_context(headers)
    return headers
