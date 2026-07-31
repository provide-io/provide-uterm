#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Component builders for the hosted terminal server application factory.

Cohesive construction helpers pulled out of ``create_server_app``: the
startup durability-posture logging, identity-provider selection, governance
webhook gates, and recording-store selection.  Each is a pure function of the
server config (plus a couple of already-built collaborators), returning the
constructed object(s) that ``factory_impl`` threads into the hub/registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from provide.telemetry import get_logger
from provide.uterm.server.app.factory_sweeps import _detect_multi_replica_environment
from provide.uterm.server.audit_chain import GENESIS_HASH, AuditChain, verify_audit_log
from provide.uterm.server.auth import LocalIdentityProvider, WebhookIdentityProvider

if TYPE_CHECKING:
    from collections.abc import Callable

    from provide.uterm.control.plane import ControlPlane as SharedControlPlane
    from provide.uterm.recording import InMemoryRecordingStore, LocalFileRecordingStore, NullRecordingStore
    from provide.uterm.server.api_keys import ApiKeyStore
    from provide.uterm.server.bridge.hub.ext import (
        BehavioralThresholds,
        WebhookBehavioralAuditGate,
        WebhookFanOutPolicyGate,
        WebhookPolicyGate,
        WebhookTelemetrySink,
    )
    from provide.uterm.server.bridge.identity import IdentityProvider
    from provide.uterm.server.models import ServerConfig
    from provide.uterm.server.recording import WebhookRecordingStore

logger = get_logger(__name__)


def initial_metrics() -> dict[str, int]:
    """Return a fresh zeroed per-server metrics counter map."""
    return {
        "http_requests_total": 0,
        "http_requests_4xx_total": 0,
        "http_requests_5xx_total": 0,
        "http_requests_error_total": 0,
        "auth_failures_http_total": 0,
        "auth_failures_ws_total": 0,
        "ws_disconnect_total": 0,
        "ws_disconnect_worker_total": 0,
        "ws_disconnect_browser_total": 0,
        "hijack_conflicts_total": 0,
        "hijack_lease_expiries_total": 0,
        "hijack_acquires_total": 0,
        "hijack_releases_total": 0,
        "hijack_steps_total": 0,
        # Rate-limit drop counters (websocket browser + REST acquire/send/step)
        "ws_browser_rate_limited_total": 0,
        "ws_browser_control_rate_limited_total": 0,
        # Inbound worker control frame that failed type validation (finding #5d)
        "ws_worker_frame_invalid_total": 0,
        "rest_acquire_rate_limited_total": 0,
        "rest_send_rate_limited_total": 0,
        "rest_step_rate_limited_total": 0,
        # Webhook delivery failure counters
        "webhook_delivery_blocked_total": 0,
        # Loopback destination refused because the session is tunnel-shared
        # right now. Separate from ``webhook_delivery_blocked_total`` on purpose:
        # this one is transient (it clears when the share expires) and never
        # auto-unregisters the webhook, so an operator seeing deliveries stop
        # can tell "suppressed while shared" from "destination has gone bad".
        "webhook_delivery_blocked_tunnel_total": 0,
        "webhook_auto_unregistered_total": 0,
        "webhook_delivery_failed_total": 0,
        "webhook_delivery_giving_up_total": 0,
        # Event-bus subscriber drop counter
        "event_bus_subscriber_drop_total": 0,
    }


def log_durability_posture(config: ServerConfig) -> None:
    """Emit the startup durability-warning lines for the active control plane."""
    if config.control_plane.backend == "memory":
        logger.warning(
            "standalone_server_durability=process-local: the FastAPI reference server keeps live control-plane state "
            "in memory only (tunnel tokens/share state, approvals, resume state, webhook registrations, and live "
            "session arbitration state). It is not HA or persistent across restart/failover; run it as a single active "
            "instance or use a durable backend for multi-node deployment."
        )
        # Escalate when common multi-replica orchestrators are detected.
        # Process-local control-plane state diverges across replicas: a
        # share token issued on pod A won't validate on pod B, an approval
        # decision on pod A is invisible to pod B, etc. Operators routinely
        # miss this until users hit it in prod, so emit a load-bearing
        # ERROR when the environment looks multi-replica.
        _replica_hints = _detect_multi_replica_environment()
        if _replica_hints:
            logger.error(
                "standalone_server_durability=process-local in a multi-replica environment (%s). "
                "Tunnel tokens, approvals, webhook registrations, and live runtime state are NOT replicated; "
                "share/control URLs issued on one replica will NOT authenticate against another. "
                "Pin to a single replica or move to a durable backend (control_plane.backend=sqlite/postgres).",
                ", ".join(sorted(_replica_hints)),
            )
    else:
        logger.info(
            "standalone_server_durability=sqlite: the resume-token store is "
            "persisted to %s. Session records, approvals, and hijack leases are in-memory and lost on restart; "
            "tunnel tokens, webhook registrations, fan-out groups, and live runtime state also remain process-local; "
            "see /api/durability/capabilities.",
            config.control_plane.database_url,
        )


def build_identity_provider(config: ServerConfig, api_key_store: ApiKeyStore) -> IdentityProvider:
    """Select the configured identity provider (webhook IdP or local)."""
    if config.auth.identity_provider == "webhook" and config.auth.webhook_idp_url:
        # 1d: curate the request headers/cookies forwarded to the external IdP —
        # the always-needed auth credentials plus any operator extensions. Header
        # keys are lower-cased (Starlette/httpx lower-case them); cookies match
        # by exact name.
        forward_headers = {
            "authorization",
            "x-api-key",
            config.auth.principal_header.lower(),
            config.auth.role_header.lower(),
        } | {h.lower() for h in config.auth.webhook_idp_forward_headers}
        forward_cookies = {
            config.auth.token_cookie,
            config.auth.principal_cookie,
            config.auth.role_cookie,
        } | set(config.auth.webhook_idp_forward_cookies)
        return WebhookIdentityProvider(
            url=config.auth.webhook_idp_url,
            secret=config.auth.webhook_idp_secret,
            timeout_s=config.auth.webhook_idp_timeout_s,
            on_failure=getattr(config.auth, "webhook_idp_on_failure", "deny"),
            require_signed_response=config.auth.webhook_idp_require_signed_response,
            forward_headers=frozenset(forward_headers),
            forward_cookies=frozenset(forward_cookies),
            require_response_nonce=config.auth.webhook_idp_require_response_nonce,
        )
    return LocalIdentityProvider(config.auth, api_key_store=api_key_store)


@dataclass(frozen=True, slots=True)
class GovernanceGates:
    """The optional governance webhook gates wired into the hub."""

    policy_gate: WebhookPolicyGate | None
    fanout_policy_gate: WebhookFanOutPolicyGate | None
    behavioral_audit_gate: WebhookBehavioralAuditGate | None
    behavioral_thresholds: BehavioralThresholds
    telemetry_sink: WebhookTelemetrySink | None


def build_governance_gates(config: ServerConfig) -> GovernanceGates:
    """Construct the policy / behavioral-audit / telemetry webhook gates."""
    from provide.uterm.server.bridge.hub.ext import (
        BehavioralThresholds,
        WebhookBehavioralAuditGate,
        WebhookFanOutPolicyGate,
        WebhookPolicyGate,
        WebhookTelemetrySink,
    )

    policy_gate = None
    fanout_policy_gate = None
    if config.governance.policy_webhook_url:
        policy_gate = WebhookPolicyGate(
            url=config.governance.policy_webhook_url,
            secret=config.governance.policy_webhook_secret,
            timeout_s=config.governance.policy_webhook_timeout_s,
        )
        fanout_policy_gate = WebhookFanOutPolicyGate(
            url=config.governance.policy_webhook_url,
            secret=config.governance.policy_webhook_secret,
            timeout_s=config.governance.policy_webhook_timeout_s,
        )

    behavioral_audit_gate = None
    if config.governance.behavioral_audit_url:
        behavioral_audit_gate = WebhookBehavioralAuditGate(
            url=config.governance.behavioral_audit_url,
            secret=config.governance.behavioral_audit_secret,
            fail_open=config.governance.behavioral_fail_open,
        )
    behavioral_thresholds = BehavioralThresholds(
        max_cps=config.governance.behavioral_max_cps,
        min_jitter=config.governance.behavioral_min_jitter,
    )

    telemetry_sink = None
    if config.governance.telemetry_webhook_url:
        telemetry_sink = WebhookTelemetrySink(
            url=config.governance.telemetry_webhook_url,
            secret=config.governance.telemetry_webhook_secret,
            timeout_s=config.governance.telemetry_webhook_timeout_s,
        )

    return GovernanceGates(
        policy_gate=policy_gate,
        fanout_policy_gate=fanout_policy_gate,
        behavioral_audit_gate=behavioral_audit_gate,
        behavioral_thresholds=behavioral_thresholds,
        telemetry_sink=telemetry_sink,
    )


def build_recording_store(
    config: ServerConfig,
) -> LocalFileRecordingStore | InMemoryRecordingStore | NullRecordingStore | WebhookRecordingStore:
    """Select the configured recording store backend."""
    from provide.uterm.recording import InMemoryRecordingStore, LocalFileRecordingStore, NullRecordingStore
    from provide.uterm.server.recording import WebhookRecordingStore

    if config.recording.store_type == "webhook" and config.recording.webhook_url:
        return WebhookRecordingStore(
            url=config.recording.webhook_url,
            secret=config.recording.webhook_secret,
            timeout_s=config.recording.webhook_timeout_s,
        )
    if config.recording.store_type == "memory":
        return InMemoryRecordingStore()
    if config.recording.store_type == "null":
        return NullRecordingStore()
    return LocalFileRecordingStore(config.recording.directory)


async def resume_audit_chain(
    config: ServerConfig,
    control_plane: SharedControlPlane,
    *,
    audit_event: Callable[..., object],
) -> AuditChain:
    """Resume + integrity-verify the WORM audit chain and install it globally.

    Verifies the on-disk log against the persisted control-plane head, emits a
    CRITICAL alarm + audit event on tamper/rollback (booting anyway so a
    corrupted log can't DoS startup), then resumes from the file's actual head,
    installs the chain via ``configure_audit_chain``, and re-checkpoints the
    resumed head (monotonic).  ``audit_event`` is injected so a test-time patch
    of ``factory_impl.audit_event`` is observed.
    """
    from provide.uterm.server.audit import configure_audit_chain

    assert config.audit.chain_file is not None
    cp_head = await control_plane.get_audit_head()
    # Startup integrity check: verify the on-disk log against the head.
    result = verify_audit_log(config.audit.chain_file, expected_head=cp_head)
    # Alarm predicate: a brand-new deployment (no persisted head AND no file
    # yet) is the legitimate genesis case — verify reports ok=False only because
    # the file is absent, so don't false-alarm. Alarm when the control-plane
    # head exists (rollback/truncation possible) OR the file exists but is
    # internally broken.
    file_exists = Path(config.audit.chain_file).exists()
    if not result.ok and (cp_head is not None or file_exists):
        # LOUD alarm — tamper or end-truncation/rollback detected. Boot anyway
        # (refusing to boot would let an attacker DoS by corrupting the log),
        # but emit a CRITICAL log + an audit event so monitoring fires.
        logger.critical(
            "audit_chain_integrity_alarm reason=%s first_bad_seq=%s",
            result.reason,
            result.first_bad_seq,
        )
        audit_event(
            "audit.chain_integrity_alarm",
            detail={"reason": result.reason, "first_bad_seq": result.first_bad_seq},
        )
    # Resume from the file's ACTUAL head so the forward chain stays valid.
    resume_seq = result.head_seq or 0
    resume_hash = result.head_hash or GENESIS_HASH
    audit_chain = AuditChain(config.audit.chain_file, seq=resume_seq, last_hash=resume_hash)
    configure_audit_chain(audit_chain)
    # Re-checkpoint the resumed head (monotonic — a no-op if it's behind).
    await control_plane.set_audit_head(resume_seq, resume_hash)
    return audit_chain
