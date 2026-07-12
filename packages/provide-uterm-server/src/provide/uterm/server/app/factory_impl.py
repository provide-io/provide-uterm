#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastAPI application factory for the hosted terminal server."""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketException, status
from starlette.requests import HTTPConnection  # noqa: TC002

from provide.telemetry import get_logger
from provide.uterm.server.api_keys import ApiKeyStore
from provide.uterm.server.app.auth import (
    _validate_auth_config,
    _validate_environment_profile,
    _validate_security_config,
)
from provide.uterm.server.app.connectors import _register_builtin_connectors
from provide.uterm.server.app.control_plane import _build_control_plane, _build_durability_capabilities
from provide.uterm.server.app.factory_components import (
    build_governance_gates,
    build_identity_provider,
    build_recording_store,
    initial_metrics,
    log_durability_posture,
    resume_audit_chain,
)
from provide.uterm.server.app.factory_sweeps import (
    _detect_multi_replica_environment,
    cancel_and_drain,
    checkpoint_audit_head,
    node_registry_heartbeat,
    sweep_control_plane_reap,
    sweep_expired_approvals,
    sweep_expired_recordings,
    sweep_expired_sessions,
    sweep_expired_tunnel_tokens,
    sweep_idle_sessions,
)
from provide.uterm.server.app.factory_tunnel_auth import (
    resolve_tunnel_share_principal,
    resolve_tunnel_ws_worker_principal,
)
from provide.uterm.server.app.hub_authz import build_require_hub_route_authz
from provide.uterm.server.app.middleware import (
    install_cors_security_telemetry,
    install_request_logging_middleware,
)
from provide.uterm.server.app.posture import compute_security_posture
from provide.uterm.server.app.routes_wiring import install_routers, mount_frontend_assets
from provide.uterm.server.audit import audit_event, configure_audit_chain
from provide.uterm.server.auth import (
    LocalIdentityProvider,
    extract_bearer_token,
    resolve_http_principal,
    resolve_ws_principal,
)
from provide.uterm.server.bridge.hub import ControlPlaneResumeStore, EventBus, ResumeSession, TermHub
from provide.uterm.server.bridge.identity import Principal
from provide.uterm.server.graphical import GraphicalTargetRegistry
from provide.uterm.server.policy import SessionPolicyResolver
from provide.uterm.server.profiles import FileProfileStore
from provide.uterm.server.registry import SessionRegistry
from provide.uterm.server.webhooks import WebhookManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import Protocol

    from provide.uterm.server.audit_chain import AuditChain
    from provide.uterm.server.bridge.hub.resume import _ControlPlaneResumeBackend
    from provide.uterm.server.bridge.identity import IdentityProvider
    from provide.uterm.server.models import ServerConfig

    class _GateClient(Protocol):
        """Structural type for a governance webhook gate that pools an HTTP client."""

        async def aclose(self) -> None: ...


# ``_detect_multi_replica_environment`` is re-exported (it now lives in
# ``factory_sweeps``) to keep this module's public import surface unchanged.
__all__ = ["_detect_multi_replica_environment", "create_server_app"]

logger = get_logger(__name__)
# Delay between FastAPI startup completing and the auto-start session loop
# beginning.  Gives the event loop time to finish route/middleware init.
_AUTO_START_DELAY_S = 0.15

_SHARE_SESSION_PATTERNS = (
    re.compile(r"^/api/sessions/(?P<session_id>[\w\-]+)(?:/.*)?$"),
    # ``inspect`` is included so HTTP-tunnel share cookies reach the inspector
    # page that the CLI documents.  Without it, /app/inspect/{id} falls through
    # to normal JWT auth and returns 401 even after a valid invite bootstrap.
    re.compile(r"^/app/(?:session|operator|replay|inspect)/(?P<session_id>[\w\-]+)$"),
    re.compile(r"^/ws/browser/(?P<session_id>[\w\-]+)/term$"),
    re.compile(r"^/worker/(?P<session_id>[\w\-]+)/hijack(?:/.*)?$"),
)


async def _aclose_webhook_gates(*gates: _GateClient | None) -> None:
    """Release the pooled HTTP client held by each configured webhook gate.

    Extracted from the app lifespan deliberately: coverage.py on Python 3.11
    mis-tracks branch arcs in the async-generator *resume* after an awaited call
    (the same bug the ``hub.shutdown()`` pragma below works around), so a
    ``for``/``if``/``await`` loop placed in that continuation reads as uncovered
    on 3.11 even when every app-shutdown test drives it. Living in an ordinary
    helper, the loop is tracked correctly on every interpreter and its two
    branches (gate present / gate absent) are directly unit-testable.
    """
    for gate in gates:
        if gate is not None:
            await gate.aclose()


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
    _validate_security_config(config)
    _validate_environment_profile(config)
    _api_only_env = os.environ.get("UTERM_API_ONLY", "").strip().lower() in {"1", "true", "yes"}
    if not api_only and not _api_only_env:
        _app_pkg._validate_frontend_assets()

    # One structured startup line summarizing the effective security posture
    # (declared environment, bind host, auth mode, active dev opt-outs, and the
    # single ``secure`` boolean) so operators see in one glance whether a config
    # copied between environments still represents a hardened deployment.
    _posture = compute_security_posture(config)
    logger.info(
        "security_posture environment=%s bind=%s auth_mode=%s dev_opt_outs=%s secure=%s",
        _posture["environment"],
        _posture["bind_host"],
        _posture["auth_mode"],
        _posture["dev_opt_outs"],
        _posture["secure"],
    )

    durability_capabilities = _build_durability_capabilities(config).as_dict()

    log_durability_posture(config)

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
    graphical_target_registry: GraphicalTargetRegistry | None = None
    metrics: dict[str, int] = initial_metrics()
    # Token state values are heterogeneous (str token values, float expiries,
    # int counters); the registry expects ``dict[str, object]`` per-session.
    tunnel_tokens: dict[str, dict[str, object]] = {}
    tunnel_invites: dict[str, dict[str, object]] = {}

    def _inc_metric(name: str, value: int = 1) -> None:
        metrics[name] = metrics.get(name, 0) + value

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
                return Principal.anonymous()
            return resolved

        share_principal = resolve_tunnel_share_principal(connection, config=config, patterns=_SHARE_SESSION_PATTERNS)
        if share_principal is not None:
            connection.state.uterm_principal = share_principal
            return

        # In JWT mode, tunnel workers authenticate with tunnel tokens before
        # falling through to JWT principal resolution.  Without this bypass,
        # per-session /tunnel/{id} tokens would be rejected as anonymous.
        if connection.scope.get("type") == "websocket":
            worker_principal = resolve_tunnel_ws_worker_principal(connection, config=config)
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
                connection.state.uterm_principal = Principal.system_worker()
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
                    principal = Principal.anonymous()
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

    # Choose Identity Provider + governance gates based on config.
    api_key_store = ApiKeyStore()
    idp: IdentityProvider = build_identity_provider(config, api_key_store)
    _gates = build_governance_gates(config)
    policy_gate = _gates.policy_gate
    behavioral_audit_gate = _gates.behavioral_audit_gate
    behavioral_thresholds = _gates.behavioral_thresholds
    telemetry_sink = _gates.telemetry_sink

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
        worker_frame_on_invalid=config.worker_frame_on_invalid,
        resume_store=resume_store,
        on_resume=_on_resume,
        browser_rate_limit_per_sec=config.browser_rate_limit_per_sec,
        max_connections_per_principal=config.max_connections_per_principal,
        max_workers=config.max_workers,
        event_bus=EventBus(on_metric=_inc_metric),
        policy_gate=policy_gate,
        identity_provider=idp,
        delegate_roles=getattr(config.auth, "delegate_roles", True),
        behavioral_audit_gate=behavioral_audit_gate,
        behavioral_thresholds=behavioral_thresholds,
        behavioral_audit_interval_s=config.governance.behavioral_audit_interval_s,
        telemetry_sink=telemetry_sink,
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

    recording_store = build_recording_store(config)

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
        block_private_connector_targets=config.security.block_private_connector_targets,
        default_visibility=config.security.default_session_visibility,
    )
    profile_store = FileProfileStore(config.profiles.directory)

    @asynccontextmanager
    async def _running_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal graphical_target_registry

        async def _delayed_boot() -> None:
            # Yield to the event loop so FastAPI finishes its own startup tasks
            # (route registration, middleware init) before we connect sessions.
            await asyncio.sleep(_AUTO_START_DELAY_S)
            await registry.start_auto_start_sessions()

        await control_plane.migrate()
        graphical_target_registry = GraphicalTargetRegistry(config.graphical_targets, control_plane)
        _app.state.uterm_graphical_target_registry = graphical_target_registry

        # WORM audit chain (opt-in): resume+verify the chain, then start the
        # periodic head checkpoint.  ``audit_event`` is threaded so the resume
        # helper observes any test-time patch of ``factory_impl.audit_event``.
        audit_chain: AuditChain | None = None
        audit_checkpoint_task: asyncio.Task[None] | None = None
        if config.audit.chain_enabled and config.audit.chain_file:
            audit_chain = await resume_audit_chain(config, control_plane, audit_event=audit_event)
            audit_checkpoint_task = asyncio.create_task(
                checkpoint_audit_head(audit_chain, config=config, control_plane=control_plane)
            )

        boot_task = asyncio.create_task(_delayed_boot())
        boot_task.add_done_callback(
            lambda t: (
                logger.error("auto_start_sessions_failed error=%s", t.exception())
                if not t.cancelled() and t.exception() is not None
                else None
            )
        )
        sweep_task = asyncio.create_task(
            sweep_expired_tunnel_tokens(tunnel_tokens=tunnel_tokens, tunnel_invites=tunnel_invites)
        )
        approval_sweep_task = asyncio.create_task(sweep_expired_approvals(hub=hub))
        idle_sweep_task = asyncio.create_task(sweep_idle_sessions(config=config, hub=hub))
        retention_sweep_task = asyncio.create_task(sweep_expired_sessions(config=config, registry=registry))
        recording_retention_sweep_task = asyncio.create_task(sweep_expired_recordings(config=config))
        heartbeat_task = asyncio.create_task(node_registry_heartbeat(config=config, hub=hub))
        # Both backends soft-delete (set deleted_at/revoked_at/resolved_at and
        # leave expired rows in place), so both need the reaper to physically
        # prune past the retention cutoff. Schedule it unconditionally.
        reap_task = asyncio.create_task(sweep_control_plane_reap(config=config, control_plane=control_plane))
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
            if audit_checkpoint_task is not None:
                # The task and the chain are created together in the startup
                # block above, so the task being set implies the chain is too
                # (narrows AuditChain | None for the head flush below).
                assert audit_chain is not None
                audit_checkpoint_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await audit_checkpoint_task
                # Flush the latest head on clean shutdown so the persisted
                # anti-rollback anchor reflects every record written this run.
                with contextlib.suppress(Exception):
                    await control_plane.set_audit_head(audit_chain.seq, audit_chain.last_hash)
                # Reset the module global so a re-created app in the same process
                # starts clean (no stale chain).
                configure_audit_chain(None)
            await cancel_and_drain(
                reap_task,
                heartbeat_task,
                recording_retention_sweep_task,
                retention_sweep_task,
                idle_sweep_task,
                approval_sweep_task,
                sweep_task,
                boot_task,
            )
            # coverage.py on Python 3.11 mis-tracks this async-generator resume after
            # the awaited cancel_and_drain (covered on 3.12+; run by every app-shutdown test).
            await hub.shutdown()  # pragma: no cover
            await webhook_manager.shutdown()
            # Release the webhook authorization provider's pooled HTTP client
            # (no-op for the local RBAC default, which holds no resources).
            await authz.aclose()
            # Release the pooled HTTP clients held by the governance webhook gates.
            # Delegated to a module-level helper so the branchy close loop is
            # tracked correctly on Python 3.11 (see ``_aclose_webhook_gates``).
            await _aclose_webhook_gates(policy_gate, behavioral_audit_gate, telemetry_sink)
            await registry.shutdown()
            if graphical_target_registry is not None:
                await graphical_target_registry.close()
            await control_plane.close()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            async with _running_lifespan(_app):
                yield
        except BaseException:
            if graphical_target_registry is not None:
                with contextlib.suppress(BaseException):
                    await graphical_target_registry.close()
            with contextlib.suppress(BaseException):
                await control_plane.close()
            raise

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
    app.state.uterm_graphical_target_registry = graphical_target_registry
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
    # Same api_only gate as the frontend-asset presence check above: an
    # API-only/headless server must not require the (possibly unbuilt) bundled
    # UI directory to exist just to mount it.
    if not api_only and not _api_only_env:
        mount_frontend_assets(app, config=config)
    return app
