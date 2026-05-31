#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastAPI application factory for the hosted terminal server."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketException, status
from starlette.requests import HTTPConnection  # noqa: TC002

from provide.telemetry import get_logger
from provide.uterm.server.api_keys import ApiKeyStore
from provide.uterm.server.app.auth import _validate_auth_config
from provide.uterm.server.app.connectors import _register_builtin_connectors
from provide.uterm.server.app.control_plane import _build_control_plane, _build_durability_capabilities
from provide.uterm.server.app.hub_authz import build_require_hub_route_authz
from provide.uterm.server.app.middleware import (
    install_cors_security_telemetry,
    install_request_logging_middleware,
)
from provide.uterm.server.app.routes_wiring import install_routers, mount_frontend_assets
from provide.uterm.server.auth import (
    LocalIdentityProvider,
    WebhookIdentityProvider,
    extract_bearer_token,
    resolve_http_principal,
    resolve_ws_principal,
)
from provide.uterm.server.bridge.hub import ControlPlaneResumeStore, EventBus, ResumeSession, TermHub
from provide.uterm.server.bridge.identity import Principal
from provide.uterm.server.policy import SessionPolicyResolver
from provide.uterm.server.profiles import FileProfileStore
from provide.uterm.server.registry import SessionRegistry
from provide.uterm.server.tunnel_invites import sweep_expired_tunnel_invites
from provide.uterm.server.webhooks import WebhookManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from provide.uterm.server.bridge.hub.resume import _ControlPlaneResumeBackend
    from provide.uterm.server.bridge.identity import IdentityProvider
    from provide.uterm.server.models import ServerConfig

logger = get_logger(__name__)
# Delay between FastAPI startup completing and the auto-start session loop
# beginning.  Gives the event loop time to finish route/middleware init.
_AUTO_START_DELAY_S = 0.15
# Cadence for the approval-expiry sweep: time out expired PENDING approvals
# (firing on_expired so hold buffers drain) and prune old terminal entries.
# Matches the tunnel-token sweep cadence — both reap short-lived hold state.
_APPROVAL_SWEEP_INTERVAL_S = 30

# Environment variables that almost-always indicate a multi-replica
# orchestrator. Their presence alone doesn't *prove* multi-replica
# operation (a single-replica k8s deployment also sets KUBERNETES_*),
# but it raises the question loudly enough that the startup banner
# should escalate when control-plane durability is process-local.
_MULTI_REPLICA_ENV_HINTS: tuple[tuple[str, str], ...] = (
    ("KUBERNETES_SERVICE_HOST", "Kubernetes"),
    ("K_SERVICE", "Cloud Run"),
    ("WEBSITE_INSTANCE_ID", "Azure App Service"),
    ("ECS_CONTAINER_METADATA_URI", "AWS ECS"),
    ("ECS_CONTAINER_METADATA_URI_V4", "AWS ECS"),
    ("FLY_APP_NAME", "Fly.io"),
)


def _detect_multi_replica_environment() -> set[str]:
    """Return a set of orchestrator names detected from process env.

    Returns an empty set in environments that look single-replica
    (bare-metal, single docker container, single VM, dev workstation).
    """
    found: set[str] = set()
    for env_var, label in _MULTI_REPLICA_ENV_HINTS:
        if os.environ.get(env_var):
            found.add(label)
    return found


_SHARE_SESSION_PATTERNS = (
    re.compile(r"^/api/sessions/(?P<session_id>[\w\-]+)(?:/.*)?$"),
    # ``inspect`` is included so HTTP-tunnel share cookies reach the inspector
    # page that the CLI documents.  Without it, /app/inspect/{id} falls through
    # to normal JWT auth and returns 401 even after a valid invite bootstrap.
    re.compile(r"^/app/(?:session|operator|replay|inspect)/(?P<session_id>[\w\-]+)$"),
    re.compile(r"^/ws/browser/(?P<session_id>[\w\-]+)/term$"),
    re.compile(r"^/worker/(?P<session_id>[\w\-]+)/hijack(?:/.*)?$"),
)


def create_server_app(
    config: ServerConfig,
    hub_class: type[TermHub] | None = None,
    *,
    api_only: bool = False,
) -> FastAPI:
    """Create the standalone reference server application.

    Args:
        config: Server configuration.
        hub_class: Optional TermHub subclass to use instead of the default TermHub.
                   Useful for injecting mixins such as DeckMuxMixin.
        api_only: When True (or ``UTERM_API_ONLY=1`` is set in the environment),
                  skip the frontend-asset presence check.  Useful for headless /
                  API-only deployments and unit tests that don't need the UI.
    """
    import os

    # Look up _validate_frontend_assets via the package namespace so that
    # tests patching ``provide.uterm.server.app._validate_frontend_assets``
    # intercept the call here.
    from provide.uterm.server import app as _app_pkg

    _register_builtin_connectors(config)
    _validate_auth_config(config)
    _api_only_env = os.environ.get("UTERM_API_ONLY", "").strip().lower() in {"1", "true", "yes"}
    if not api_only and not _api_only_env:
        _app_pkg._validate_frontend_assets()

    durability_capabilities = _build_durability_capabilities(config).as_dict()

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
            "standalone_server_durability=sqlite: shared control-plane stores (sessions, resume tokens) are "
            "persisted to %s. Approvals and hijack leases are in-memory and lost on restart; tunnel tokens, "
            "webhook registrations, fan-out groups, and live runtime state also remain process-local; "
            "see /api/durability/capabilities.",
            config.control_plane.database_url,
        )

    from provide.uterm.server.authorization import (
        AuthorizationProvider,
        AuthorizationService,
        LocalAuthorizationProvider,
        WebhookAuthorizationProvider,
    )

    authz_provider: AuthorizationProvider = LocalAuthorizationProvider()
    if config.governance.authz_webhook_url:
        authz_provider = WebhookAuthorizationProvider(
            url=config.governance.authz_webhook_url,
            secret=config.governance.authz_webhook_secret,
            timeout_s=config.governance.authz_webhook_timeout_s,
        )
    authz = AuthorizationService(authz_provider)
    policy = SessionPolicyResolver(config.auth, authz=authz)
    registry: SessionRegistry | None = None
    metrics: dict[str, int] = {
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
        "rest_acquire_rate_limited_total": 0,
        "rest_send_rate_limited_total": 0,
        "rest_step_rate_limited_total": 0,
        # Webhook delivery failure counters
        "webhook_delivery_blocked_total": 0,
        "webhook_auto_unregistered_total": 0,
        "webhook_delivery_failed_total": 0,
        "webhook_delivery_giving_up_total": 0,
        # Event-bus subscriber drop counter
        "event_bus_subscriber_drop_total": 0,
    }
    # Token state values are heterogeneous (str token values, float expiries,
    # int counters); the registry expects ``dict[str, object]`` per-session.
    tunnel_tokens: dict[str, dict[str, object]] = {}
    tunnel_invites: dict[str, dict[str, object]] = {}

    def _inc_metric(name: str, value: int = 1) -> None:
        metrics[name] = metrics.get(name, 0) + value

    def _share_session_id_for(path: str) -> str | None:
        for pattern in _SHARE_SESSION_PATTERNS:
            match = pattern.match(path)
            if match is not None:
                return str(match.group("session_id"))
        return None

    def _resolve_tunnel_share_principal(connection: HTTPConnection) -> Principal | None:
        path = str(connection.scope.get("path", ""))
        session_id = _share_session_id_for(path)
        if session_id is None:
            return None
        # Tunnel share/control auth is cookie-only after the one-time
        # ``?invite=`` bootstrap. Do not accept raw bearer tokens in the query
        # string; URLs are routinely logged by proxies and browser history.
        provided = None
        from http.cookies import SimpleCookie

        app = connection.scope.get("app")
        token_map = getattr(getattr(app, "state", object()), "uterm_tunnel_tokens", {})
        token_state = token_map.get(session_id) if isinstance(token_map, dict) else None
        if token_state is None:
            return None

        cookie_header = dict(connection.scope.get("headers", [])).get(b"cookie", b"").decode("utf-8", errors="ignore")
        cookies = SimpleCookie(cookie_header)
        cookie_key = f"uterm_tunnel_{session_id}"
        if cookie_key in cookies:
            provided = cookies[cookie_key].value
        if not provided:
            return None
        # Check expiry.
        expires_at = token_state.get("expires_at")
        if isinstance(expires_at, (int, float)) and time.time() > float(expires_at):
            logger.info("tunnel_token_expired session_id=%s", session_id)
            return None
        # Check IP binding.
        if config.tunnel.ip_binding:
            issued_ip = token_state.get("issued_ip")
            client_ip = str((connection.scope.get("client") or ("unknown", 0))[0])
            if issued_ip and issued_ip != client_ip:
                logger.info(
                    "tunnel_token_ip_mismatch session_id=%s issued=%s actual=%s", session_id, issued_ip, client_ip
                )
                return None
        # Match token type. The stored values are BLAKE2b digests, so we
        # compare the hash of the caller-supplied token against the stored
        # hash in constant time — see ``tunnel/token_hash.py``.
        from provide.uterm.tunnel.token_hash import verify_token

        source_ip = str((connection.scope.get("client") or ("unknown", 0))[0])
        if verify_token(str(provided), str(token_state.get("control_token_hash", ""))):
            connection.state.uterm_share_token = str(provided)
            connection.state.uterm_share_role = "operator"
            logger.info("tunnel_token_validated session_id=%s token_type=control source_ip=%s", session_id, source_ip)
            return Principal(
                subject_id=f"share:{session_id}:operator",
                roles=frozenset({"admin"}),
                scopes=frozenset({"*"}),
                # Confine the admin grant to this share's session: the operator
                # drives its own session with full admin capabilities but is not
                # a global administrator, so the grant cannot escalate to other
                # sessions even if this principal is resolved off-path.
                admin_session_scope=session_id,
            )
        if verify_token(str(provided), str(token_state.get("share_token_hash", ""))):
            connection.state.uterm_share_token = str(provided)
            connection.state.uterm_share_role = "viewer"
            logger.info("tunnel_token_validated session_id=%s token_type=share source_ip=%s", session_id, source_ip)
            return Principal(
                subject_id=f"share:{session_id}:viewer",
                roles=frozenset({"viewer"}),
                scopes=frozenset({"session.read"}),
            )
        logger.info("tunnel_token_validation_failed session_id=%s source_ip=%s", session_id, source_ip)
        return None

    def _resolve_tunnel_ws_worker_principal(connection: HTTPConnection) -> Principal | None:
        path = str(connection.scope.get("path", ""))
        if not path.startswith("/tunnel/"):
            return None
        worker_id = path.removeprefix("/tunnel/")
        if not worker_id:  # pragma: no cover — FastAPI's path matcher already excludes the empty-id case
            return None

        provided = extract_bearer_token(connection.headers)
        if not provided:  # pragma: no cover — WS upgrade with no Authorization header already 401s upstream
            return None

        # Tunnel workers in JWT mode should still be able to authenticate with
        # the raw global worker token.  This keeps CLI/runtime behaviour
        # aligned with /ws/worker/ auth before JWT resolution is attempted.
        if config.auth.worker_bearer_token is not None and secrets.compare_digest(
            provided,
            config.auth.worker_bearer_token,
        ):
            connection.state.uterm_worker_token = provided
            return Principal(subject_id="worker", roles=frozenset({"admin"}), scopes=frozenset({"*"}))

        app = connection.scope.get("app")
        token_map = getattr(getattr(app, "state", object()), "uterm_tunnel_tokens", {})
        token_state = token_map.get(worker_id) if isinstance(token_map, dict) else None
        if not isinstance(token_state, dict):
            return None

        # Check expiry.
        expires_at = token_state.get("expires_at")
        if isinstance(expires_at, (int, float)) and time.time() > float(expires_at):
            logger.info("tunnel_token_expired session_id=%s", worker_id)
            return None

        # Check IP binding.
        if config.tunnel.ip_binding:
            issued_ip = token_state.get("issued_ip")
            client_ip = str((connection.scope.get("client") or ("unknown", 0))[0])
            if (
                issued_ip and issued_ip != client_ip
            ):  # pragma: no branch — matching-IP happy-path is covered by tunnel auth tests
                logger.info(
                    "tunnel_token_ip_mismatch session_id=%s issued=%s actual=%s", worker_id, issued_ip, client_ip
                )
                return None

        # Match token type (stored as BLAKE2b digest).
        from provide.uterm.tunnel.token_hash import verify_token

        source_ip = str((connection.scope.get("client") or ("unknown", 0))[0])
        if verify_token(str(provided), str(token_state.get("worker_token_hash", ""))):
            connection.state.uterm_worker_token = str(provided)
            logger.info("tunnel_worker_token_validated session_id=%s source_ip=%s", worker_id, source_ip)
            return Principal(subject_id="worker", roles=frozenset({"admin"}), scopes=frozenset({"*"}))

        logger.info("tunnel_worker_token_validation_failed session_id=%s source_ip=%s", worker_id, source_ip)
        return None

    async def _require_authenticated(connection: HTTPConnection) -> None:
        async def _resolve_configured_principal(connection: HTTPConnection) -> Principal:
            # Keep the existing local-auth resolver path (including API-key
            # store lookups) and route webhook mode through the configured IDP.
            if isinstance(idp, LocalIdentityProvider):
                if connection.scope.get("type") == "websocket":
                    return await resolve_ws_principal(cast("WebSocket", connection), config.auth)
                return await resolve_http_principal(cast("Request", connection), config.auth)

            resolved = await idp.resolve_principal(cast("Request | WebSocket", connection))
            if resolved is None:
                return Principal(subject_id="anonymous", roles=frozenset({"viewer"}), scopes=frozenset())
            return resolved

        share_principal = _resolve_tunnel_share_principal(connection)
        if share_principal is not None:
            connection.state.uterm_principal = share_principal
            return

        # In JWT mode, tunnel workers authenticate with tunnel tokens before
        # falling through to JWT principal resolution.  Without this bypass,
        # per-session /tunnel/{id} tokens would be rejected as anonymous.
        if connection.scope.get("type") == "websocket":
            worker_principal = _resolve_tunnel_ws_worker_principal(connection)
            if worker_principal is not None:
                connection.state.uterm_principal = worker_principal
                return

        # Workers authenticate with a raw bearer token, not a JWT.  Check it
        # before JWT resolution so a valid worker token is never mis-rejected as
        # anonymous when auth.mode='jwt'.
        if (
            config.auth.worker_bearer_token
            and connection.scope.get("type") == "websocket"
            and str(connection.scope.get("path", "")).startswith("/ws/worker/")
        ):
            token = extract_bearer_token(connection.headers)
            if secrets.compare_digest(token or "", config.auth.worker_bearer_token or ""):
                connection.state.uterm_principal = Principal(
                    subject_id="worker", roles=frozenset({"admin"}), scopes=frozenset({"*"})
                )
                return
        if connection.scope.get("type") == "websocket":
            principal = await _resolve_configured_principal(connection)
            connection.state.uterm_principal = principal
            if principal.subject_id == "anonymous":
                _inc_metric("auth_failures_ws_total")
                logger.info("authn_denied surface=websocket")
                raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="authentication required")
            return
        principal = await _resolve_configured_principal(connection)
        connection.state.uterm_principal = principal
        if principal.subject_id == "anonymous":
            _inc_metric("auth_failures_http_total")
            logger.info("authn_denied surface=http")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")

    _require_hub_route_authz = build_require_hub_route_authz(registry_getter=lambda: registry)

    async def _on_resume(_token: str, session: ResumeSession) -> bool:
        """Reject resume if the backing session no longer exists or has been recreated."""
        if registry is None:  # pragma: no cover — always initialized before first WS connection
            return True
        session_def = await registry.get_definition(session.worker_id)
        if session_def is None:
            return False
        # Guard against delete-and-recreate: if the session was created after
        # this token was issued, it is a different session and the token is stale.
        return not (session.wall_created_at > 0 and session_def.created_at.timestamp() > session.wall_created_at)

    async def _resolve_browser_role(ws: WebSocket, worker_id: str) -> str:
        principal = getattr(ws.state, "uterm_principal", None)
        if principal is None:
            if isinstance(idp, LocalIdentityProvider):
                principal = await resolve_ws_principal(ws, config.auth)
            else:
                principal = await idp.resolve_principal(ws)
                if principal is None:
                    principal = Principal(subject_id="anonymous", roles=frozenset({"viewer"}), scopes=frozenset())
        session = await registry.get_definition(worker_id) if registry is not None else None
        if session is None:
            # No registered SessionDefinition (worker connected ad-hoc). There
            # is no visibility policy to consult, so fail closed: only a global
            # admin may observe an unregistered worker. Operators/viewers are
            # rejected unless the operator explicitly opts in.
            if "admin" in principal.roles:
                return "admin"
            if config.auth.allow_adhoc_browser_observers:
                if "operator" in principal.roles:
                    return "operator"
                return "viewer"
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="insufficient privileges")
        if not await authz.can_read_session(principal, session):
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="insufficient privileges")
        return await policy.role_for(principal, session)

    from provide.uterm.server.bridge.hub.ext import (
        BehavioralThresholds,
        WebhookBehavioralAuditGate,
        WebhookPolicyGate,
    )

    policy_gate = None
    if config.governance.policy_webhook_url:
        policy_gate = WebhookPolicyGate(
            url=config.governance.policy_webhook_url,
            secret=config.governance.policy_webhook_secret,
            timeout_s=config.governance.policy_webhook_timeout_s,
        )

    # Choose Identity Provider based on config
    api_key_store = ApiKeyStore()

    if config.auth.identity_provider == "webhook" and config.auth.webhook_idp_url:
        idp: IdentityProvider = WebhookIdentityProvider(
            url=config.auth.webhook_idp_url,
            secret=config.auth.webhook_idp_secret,
            timeout_s=config.auth.webhook_idp_timeout_s,
            on_failure=getattr(config.auth, "webhook_idp_on_failure", "deny"),
        )
    else:
        idp = LocalIdentityProvider(config.auth, api_key_store=api_key_store)

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

    control_plane = _build_control_plane(config)
    # The shared ``ControlPlane`` Protocol intentionally omits the resume-only
    # ``token_store`` method (kept out of the public Protocol so embedders can
    # ship plane backends without resume support).  Concrete plane engines we
    # build here (memory + sqlite) implement it; cast for the resume backend.
    resume_store = ControlPlaneResumeStore(cast("_ControlPlaneResumeBackend", control_plane))

    _hub_class = hub_class if hub_class is not None else TermHub
    hub = _hub_class(
        resolve_browser_role=_resolve_browser_role,
        on_metric=_inc_metric,
        worker_token=config.auth.worker_bearer_token,
        resume_store=resume_store,
        on_resume=_on_resume,
        browser_rate_limit_per_sec=config.browser_rate_limit_per_sec,
        max_connections_per_principal=config.max_connections_per_principal,
        event_bus=EventBus(on_metric=_inc_metric),
        policy_gate=policy_gate,
        identity_provider=idp,
        delegate_roles=getattr(config.auth, "delegate_roles", True),
        behavioral_audit_gate=behavioral_audit_gate,
        behavioral_thresholds=behavioral_thresholds,
        behavioral_audit_interval_s=config.governance.behavioral_audit_interval_s,
    )
    # Attach the fan-out controller so routes and WS dispatch can find it.
    from provide.uterm.server.bridge.fanout import FanOutController, InMemoryFanOutStore

    setattr(hub, "fan_out_controller", FanOutController(hub=hub, store=InMemoryFanOutStore()))  # noqa: B010
    webhook_manager = WebhookManager(
        allow_loopback_destinations=config.webhooks.allow_loopback_destinations,
        on_metric=_inc_metric,
    )
    # Annotation detector scans snapshot/send text for security-relevant patterns.
    # Imported lazily — annotation lives in the separate provide-uterm-annotation
    # package (optional extra "annotation"). If a deployment opts out of installing
    # it, raise a clear runtime error instead of a bare ImportError.
    try:
        from provide.uterm.annotation import PatternDetector
    except ImportError as exc:  # pragma: no cover - exercised when extra is omitted
        raise RuntimeError(
            "annotation support not installed; pip install 'provide-uterm-server[annotation]'",
        ) from exc
    from provide.uterm.recording import InMemoryRecordingStore, LocalFileRecordingStore, NullRecordingStore
    from provide.uterm.server.discovery import (
        DiscoveryProvider,
        NodeStatus,
        NoOpDiscoveryProvider,
        WebhookDiscoveryProvider,
    )
    from provide.uterm.server.recording import WebhookRecordingStore

    # Choose Recording Store
    recording_store: LocalFileRecordingStore | InMemoryRecordingStore | NullRecordingStore | WebhookRecordingStore
    if config.recording.store_type == "webhook" and config.recording.webhook_url:
        recording_store = WebhookRecordingStore(
            url=config.recording.webhook_url,
            secret=config.recording.webhook_secret,
            timeout_s=config.recording.webhook_timeout_s,
        )
    elif config.recording.store_type == "memory":
        recording_store = InMemoryRecordingStore()
    elif config.recording.store_type == "null":
        recording_store = NullRecordingStore()
    else:
        recording_store = LocalFileRecordingStore(config.recording.directory)

    detector = PatternDetector()
    registry = SessionRegistry(
        config.sessions,
        hub=hub,
        public_base_url=config.server.public_base_url,
        recording=config.recording,
        recording_store=recording_store,
        worker_bearer_token=config.auth.worker_bearer_token,
        max_sessions=config.server.max_sessions,
        detector=detector,
        tunnel_tokens=tunnel_tokens,
    )
    profile_store = FileProfileStore(config.profiles.directory)

    async def _sweep_idle_sessions() -> None:
        """Periodically disconnect sessions with no activity beyond the configured timeout."""
        timeout_s = config.session_idle_timeout_s
        while True:
            await asyncio.sleep(60)
            if timeout_s <= 0:
                continue
            now = time.time()
            candidates = await hub.get_idle_candidates(timeout_s)
            for worker_id, last_at in candidates:
                try:
                    logger.info(
                        "session_idle_timeout worker_id=%s idle_s=%d",
                        worker_id,
                        int(now - last_at),
                    )
                    await hub.disconnect_worker(worker_id)
                except Exception:
                    logger.exception("session_idle_timeout_error worker_id=%s", worker_id)

    async def _sweep_expired_sessions() -> None:
        """Remove stopped sessions older than session_retention_s."""
        retention_s = config.session_retention_s
        while True:
            await asyncio.sleep(300)
            if retention_s <= 0:
                continue
            now = time.time()
            pairs = await registry.list_sessions_with_definitions()
            for sess_status, _definition in pairs:
                if sess_status.lifecycle_state != "stopped":
                    continue
                if sess_status.stopped_at is None:
                    continue
                if (now - sess_status.stopped_at) >= retention_s:
                    try:
                        await registry.delete_session(sess_status.session_id)
                        logger.info(
                            "session_retention_sweep session_id=%s age_s=%d",
                            sess_status.session_id,
                            int(now - sess_status.stopped_at),
                        )
                    except Exception:
                        logger.exception("session_retention_sweep_error session_id=%s", sess_status.session_id)

    async def _sweep_expired_recordings() -> None:
        """Delete local recording files older than recording.retention_s."""
        retention_s = config.recording.retention_s
        if retention_s <= 0 or config.recording.store_type != "local":
            while True:
                await asyncio.sleep(300)
            # unreachable; keeps cancellation/loop behavior identical
        directory = config.recording.directory
        while True:
            await asyncio.sleep(300)
            now = time.time()
            for path in directory.glob("*.jsonl"):
                try:
                    age_s = now - path.stat().st_mtime
                    if age_s >= retention_s:
                        path.unlink(missing_ok=True)
                        logger.info(
                            "recording_retention_sweep path=%s age_s=%d",
                            str(path),
                            int(age_s),
                        )
                except Exception:
                    logger.exception("recording_retention_sweep_error path=%s", str(path))

    async def _sweep_expired_tunnel_tokens() -> None:
        """Periodically remove expired tunnel tokens and pending invites."""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            sweep_expired_tunnel_invites(tunnel_invites, now=now)
            expired: list[str] = []
            for sid, state in tunnel_tokens.items():
                expires_at = state.get("expires_at")
                if isinstance(expires_at, (int, float)) and now > float(expires_at):
                    expired.append(sid)
            for sid in expired:
                tunnel_tokens.pop(sid, None)
                logger.info("tunnel_token_expired session_id=%s swept=true", sid)

    async def _sweep_expired_approvals() -> None:
        """Periodically time out expired pending approvals and prune old ones."""
        while True:
            await asyncio.sleep(_APPROVAL_SWEEP_INTERVAL_S)
            try:
                await hub.approval_store.cleanup_expired()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("approval_sweep_error")

    async def _sweep_control_plane_reap() -> None:
        """Periodically physically-delete expired/soft-deleted control-plane rows."""
        while True:
            await asyncio.sleep(config.control_plane.reap_interval_s)
            try:
                deleted = await control_plane.reap(now=time.time(), retention_s=config.control_plane.reap_retention_s)
                if deleted:
                    logger.info("control_plane_reap deleted_rows=%d", deleted)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("control_plane_reap_error")

    async def _node_registry_heartbeat() -> None:
        """Periodically announce Node status to the External Management Tier."""
        discovery_provider: DiscoveryProvider
        if config.governance.discovery_provider == "webhook" and config.governance.registry_webhook_url:
            discovery_provider = WebhookDiscoveryProvider(
                url=config.governance.registry_webhook_url,
                secret=config.governance.registry_webhook_secret,
            )
        else:
            discovery_provider = NoOpDiscoveryProvider()

        if isinstance(discovery_provider, NoOpDiscoveryProvider):
            return

        interval = config.governance.registry_webhook_interval_s
        node_id = getattr(config.server, "node_id", "default")

        while True:
            try:
                status = NodeStatus(
                    node_id=node_id,
                    active_sessions=await hub.browser_count_total(),
                    worker_count=len(hub._workers),
                    timestamp=time.time(),
                )
                await discovery_provider.announce(status)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("node_registry_heartbeat_failed")
            await asyncio.sleep(interval)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async def _delayed_boot() -> None:
            # Yield to the event loop so FastAPI finishes its own startup tasks
            # (route registration, middleware init) before we connect sessions.
            await asyncio.sleep(_AUTO_START_DELAY_S)
            await registry.start_auto_start_sessions()

        await control_plane.migrate()
        boot_task = asyncio.create_task(_delayed_boot())
        boot_task.add_done_callback(
            lambda t: (
                logger.error("auto_start_sessions_failed error=%s", t.exception())
                if not t.cancelled() and t.exception() is not None
                else None
            )
        )
        sweep_task = asyncio.create_task(_sweep_expired_tunnel_tokens())
        approval_sweep_task = asyncio.create_task(_sweep_expired_approvals())
        idle_sweep_task = asyncio.create_task(_sweep_idle_sessions())
        retention_sweep_task = asyncio.create_task(_sweep_expired_sessions())
        recording_retention_sweep_task = asyncio.create_task(_sweep_expired_recordings())
        heartbeat_task = asyncio.create_task(_node_registry_heartbeat())
        # Only the sqlite backend accumulates rows; memory hard-deletes, so it
        # needs no reaper.
        reap_task: asyncio.Task[None] | None = None
        if config.control_plane.backend == "sqlite":
            reap_task = asyncio.create_task(_sweep_control_plane_reap())
        pam_task: asyncio.Task[None] | None = None
        with contextlib.suppress(ImportError):
            from provide.uterm.server.pam_integration import run_pam_integration

            pam_task = asyncio.create_task(run_pam_integration(config, registry))
        # All startup work complete — mark the app as ready so /readyz and
        # /api/health return 200.  If migrate() raised above (before we reach
        # here), uterm_ready stays False for the lifetime of the process.
        _app.state.uterm_ready = True
        try:
            yield
        finally:
            # Signal draining readiness probes that the pod is going away.
            _app.state.uterm_ready = False
            if pam_task is not None:
                pam_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pam_task
            if reap_task is not None:
                reap_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reap_task
            heartbeat_task.cancel()
            recording_retention_sweep_task.cancel()
            retention_sweep_task.cancel()
            idle_sweep_task.cancel()
            approval_sweep_task.cancel()
            sweep_task.cancel()
            boot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            with contextlib.suppress(asyncio.CancelledError):
                await recording_retention_sweep_task
            with contextlib.suppress(asyncio.CancelledError):
                await boot_task
            with contextlib.suppress(asyncio.CancelledError):
                await sweep_task
            with contextlib.suppress(asyncio.CancelledError):
                await approval_sweep_task
            with contextlib.suppress(asyncio.CancelledError):
                await idle_sweep_task
            with contextlib.suppress(asyncio.CancelledError):
                await retention_sweep_task
            await hub.shutdown()
            await webhook_manager.shutdown()
            await registry.shutdown()
            await control_plane.close()

    app = FastAPI(title=config.server.title, lifespan=_lifespan)
    app.state.uterm_config = config
    app.state.uterm_policy = policy
    app.state.uterm_authz = authz
    app.state.uterm_hub = hub
    app.state.uterm_registry = registry
    # Readiness flag — False until the lifespan finishes migrate() + task creation.
    # /readyz and /api/health gate on this so a half-initialized pod never passes
    # a Kubernetes readinessProbe.
    app.state.uterm_ready = False
    app.state.uterm_metrics = metrics
    app.state.uterm_webhooks = webhook_manager
    app.state.uterm_profile_store = profile_store
    app.state.uterm_control_plane = control_plane
    app.state.uterm_durability_capabilities = durability_capabilities
    app.state.uterm_tunnel_tokens = tunnel_tokens
    app.state.uterm_tunnel_invites = tunnel_invites
    app.state.uterm_api_key_store = api_key_store
    app.state.uterm_idp = idp
    app.state.uterm_startup_time = time.time()

    @app.get("/api/durability/capabilities", dependencies=[Depends(_require_authenticated)])
    async def durability_capabilities_endpoint() -> dict[str, object]:
        return cast("dict[str, object]", app.state.uterm_durability_capabilities)

    install_request_logging_middleware(app, inc_metric=_inc_metric)
    install_routers(
        app,
        config=config,
        hub=hub,
        require_authenticated=_require_authenticated,
        require_hub_route_authz=_require_hub_route_authz,
    )
    install_cors_security_telemetry(app, config=config)
    mount_frontend_assets(app, config=config)
    return app
