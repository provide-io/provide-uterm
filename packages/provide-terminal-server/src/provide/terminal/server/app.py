#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastAPI application factory for the hosted terminal server."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.resources
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs

import httpx
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketException, status
from fastapi import Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import HTTPConnection  # noqa: TC002
from starlette.staticfiles import StaticFiles

from provide.telemetry import TelemetryMiddleware, get_logger
from provide.terminal.bridge.hub import ControlPlaneResumeStore, EventBus, ResumeSession, TermHub
from provide.terminal.control.plane import ControlPlane as SharedControlPlane
from provide.terminal.control.plane import ControlPlaneConfig as SharedControlPlaneConfig
from provide.terminal.control.plane.memory import MemoryControlPlane
from provide.terminal.control.plane.sqlite import SqliteControlPlane
from provide.terminal.server.api_keys import ApiKeyStore
from provide.terminal.server.auth import (
    LocalIdentityProvider,
    Principal,
    WebhookIdentityProvider,
    extract_bearer_token,
    resolve_http_principal,
    resolve_ws_principal,
)
from provide.terminal.server.authorization import AuthorizationService
from provide.terminal.server.policy import SessionPolicyResolver
from provide.terminal.server.profiles import FileProfileStore
from provide.terminal.server.registry import SessionRegistry
from provide.terminal.server.routes.api import create_api_router
from provide.terminal.server.routes.pages import create_page_router
from provide.terminal.server.routes.profiles import create_profiles_router
from provide.terminal.server.routes.approvals import create_approvals_router
from provide.terminal.server.security import SecurityHeadersMiddleware
from provide.terminal.server.webhooks import WebhookManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response

    from provide.terminal.server.models import ServerConfig

logger = get_logger(__name__)
# Delay between FastAPI startup completing and the auto-start session loop
# beginning.  Gives the event loop time to finish route/middleware init.
_AUTO_START_DELAY_S = 0.15
_SHARE_SESSION_PATTERNS = (
    re.compile(r"^/api/sessions/(?P<session_id>[\w\-]+)(?:/.*)?$"),
    # ``inspect`` is included so HTTP-tunnel share tokens reach the inspector
    # page that the CLI documents.  Without it, /app/inspect/{id}?token=...
    # falls through to normal JWT auth and returns 401 even with a valid share
    # token — a docs-vs-behavior mismatch the reviewer flagged.
    re.compile(r"^/app/(?:session|operator|replay|inspect)/(?P<session_id>[\w\-]+)$"),
    re.compile(r"^/ws/browser/(?P<session_id>[\w\-]+)/term$"),
    re.compile(r"^/worker/(?P<session_id>[\w\-]+)/hijack(?:/.*)?$"),
)


def _build_control_plane(config: ServerConfig) -> SharedControlPlane:
    shared_config = SharedControlPlaneConfig(
        backend=config.control_plane.backend,
        database_url=config.control_plane.database_url or ":memory:",
    )
    if shared_config.backend == "sqlite":
        return SqliteControlPlane(shared_config)
    return MemoryControlPlane(shared_config)


def _validate_frontend_assets() -> None:
    frontend_root = importlib.resources.files("provide.terminal.server") / "frontend"
    # Require only the critical entry points — the full file set is validated
    # at build time by scripts/verify_package_artifacts.py.
    # Accept either Vite manifest (React app built) or legacy app/boot.js.
    required = ("hijack.html", "terminal.html")
    missing = [name for name in required if not (frontend_root / name).is_file()]
    has_vite = (frontend_root / ".vite" / "manifest.json").is_file()
    has_legacy = (frontend_root / "app" / "boot.js").is_file()
    if not has_vite and not has_legacy:
        missing.append("app/boot.js or .vite/manifest.json")
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"missing required frontend assets: {joined}")


def _validate_auth_config(config: ServerConfig) -> None:
    mode = str(config.auth.mode).strip().lower()
    if mode in {"none", "dev"}:
        # Warn loudly — in dev/none mode any request can spoof any principal
        # via the X-Principal/X-Role headers.  Never expose this mode publicly.
        logger.warning(
            "auth_mode=%s: authentication is disabled — any caller can claim any identity. "
            "Do NOT expose this server on a public network in this mode.",
            mode,
        )
        return
    if mode == "header":
        if not config.auth.header_mode_acknowledged:
            raise ValueError(
                "auth.mode='header' requires auth.header_mode_acknowledged=true. "
                "This mode trusts X-Principal/X-Role from all callers — only safe behind a reverse proxy."
            )
        logger.warning(
            "auth_mode=header: trusting X-Principal/X-Role headers from all callers. "
            "This mode MUST run behind a reverse proxy that sets these headers. "
            "Direct exposure allows any client to claim any identity.",
        )
    # All authenticated modes (jwt, header, …) require a worker bearer token.
    if not config.auth.worker_bearer_token:
        raise ValueError(f"auth.worker_bearer_token is required when auth.mode='{mode}'")
    if mode != "jwt":
        return
    if not config.auth.jwt_algorithms:
        raise ValueError("auth.jwt_algorithms must not be empty when auth.mode='jwt'")
    if any(a.strip().lower() == "none" for a in config.auth.jwt_algorithms):
        raise ValueError("'none' is not permitted in auth.jwt_algorithms")
    if not config.auth.jwt_public_key_pem and not config.auth.jwt_jwks_url:
        raise ValueError("configure auth.jwt_public_key_pem or auth.jwt_jwks_url when auth.mode='jwt'")


def _register_builtin_connectors(config: ServerConfig) -> None:
    """Register standard terminal connectors and any external plugins."""
    from provide.terminal.server.connectors import register_connector

    # 1. Built-in AGPL Connectors
    with contextlib.suppress(ImportError):
        from provide.terminal.server.connectors.shell import ShellSessionConnector

        register_connector("shell", ShellSessionConnector)

    with contextlib.suppress(ImportError):
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        register_connector("ssh", SshSessionConnector)

    with contextlib.suppress(ImportError):
        from provide.terminal.server.connectors.telnet import TelnetSessionConnector

        register_connector("telnet", TelnetSessionConnector)

    with contextlib.suppress(ImportError):
        from provide.terminal.server.connectors.websocket import WebSocketSessionConnector

        register_connector("websocket", WebSocketSessionConnector)

    with contextlib.suppress(ImportError):
        from provide.terminal.shell.terminal import UshellConnector

        register_connector("ushell", UshellConnector)

    with contextlib.suppress(ImportError):
        import provide.terminal.pty.connector  # noqa: F401

    with contextlib.suppress(ImportError):
        import provide.terminal.pty.capture_connector  # noqa: F401

    # 2. External Plugin Connectors
    import importlib
    for module_path in config.governance.external_connectors:
        try:
            importlib.import_module(module_path)
            logger.info("connector_plugin_loaded module=%s", module_path)
        except ImportError:
            logger.error("connector_plugin_load_failed module=%s", module_path)


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

    _register_builtin_connectors(config)
    _validate_auth_config(config)
    _api_only_env = os.environ.get("UTERM_API_ONLY", "").strip().lower() in {"1", "true", "yes"}
    if not api_only and not _api_only_env:
        _validate_frontend_assets()

    logger.warning(
        "standalone_server_durability=process-local: the FastAPI reference server keeps live control-plane state "
        "in memory only (tunnel tokens/share state, approvals, resume state, webhook registrations, and live "
        "session arbitration state). It is not HA or persistent across restart/failover; run it as a single active "
        "instance or use a durable backend for multi-node deployment."
    )

    from provide.terminal.server.authorization import (
        AuthorizationService,
        LocalAuthorizationProvider,
        WebhookAuthorizationProvider,
    )

    authz_provider = LocalAuthorizationProvider()
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
    }
    tunnel_tokens: dict[str, dict[str, str]] = {}

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
        # The page handler always stamps the HttpOnly ``uterm_tunnel_{id}``
        # cookie on initial load (routes/pages.py:_set_page_cookies), so the
        # cookie is the primary transport for follow-on REST/WS requests that
        # no longer carry ``?token=...``.  ``token_transport`` controls whether
        # a query-string token is ALSO accepted (legacy bearer-URL flow):
        #   "cookie" — cookie only, reject query
        #   "both"   — cookie + query
        #   "query"  — treated same as "both" (legacy; frontend no longer
        #              emits ?token= after initial page load, so cookie must
        #              still be read to avoid stranding follow-on requests)
        transport = config.tunnel.token_transport
        provided = None
        from http.cookies import SimpleCookie

        cookie_header = dict(connection.scope.get("headers", [])).get(b"cookie", b"").decode("utf-8", errors="ignore")
        cookies = SimpleCookie(cookie_header)
        cookie_key = f"uterm_tunnel_{session_id}"
        if cookie_key in cookies:
            provided = cookies[cookie_key].value
        if not provided and transport in ("query", "both"):
            raw_qs = connection.scope.get("query_string", b"")
            query = raw_qs.decode("utf-8", errors="ignore") if isinstance(raw_qs, bytes) else str(raw_qs)
            provided = (parse_qs(query).get("token", [None]) or [None])[0]
        if not provided:
            return None
        app = connection.scope.get("app")
        token_map = getattr(getattr(app, "state", object()), "uterm_tunnel_tokens", {})
        token_state = token_map.get(session_id) if isinstance(token_map, dict) else None
        if token_state is None:
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
        # Match token type.
        source_ip = str((connection.scope.get("client") or ("unknown", 0))[0])
        if secrets.compare_digest(str(provided), str(token_state.get("control_token", ""))):
            connection.state.uterm_share_token = str(provided)
            connection.state.uterm_share_role = "operator"
            logger.info("tunnel_token_validated session_id=%s token_type=control source_ip=%s", session_id, source_ip)
            return Principal(
                subject_id=f"share:{session_id}:operator",
                roles=frozenset({"admin"}),
                scopes=frozenset({"*"}),
            )
        if secrets.compare_digest(str(provided), str(token_state.get("share_token", ""))):
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

    async def _require_authenticated(connection: HTTPConnection) -> None:
        share_principal = _resolve_tunnel_share_principal(connection)
        if share_principal is not None:
            connection.state.uterm_principal = share_principal
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
            # JWT mode: JWKS key fetch may make a blocking HTTP call; offload to
            # a thread pool to avoid stalling the event loop.
            principal = await resolve_ws_principal(connection, config.auth)
            connection.state.uterm_principal = principal
            if config.auth.mode not in {"none", "dev"} and principal.subject_id == "anonymous":
                _inc_metric("auth_failures_ws_total")
                logger.info("authn_denied surface=websocket")
                raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="authentication required")
            return
        principal = await resolve_http_principal(connection, config.auth)
        connection.state.uterm_principal = principal
        if config.auth.mode not in {"none", "dev"} and principal.subject_id == "anonymous":
            _inc_metric("auth_failures_http_total")
            logger.info("authn_denied surface=http")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")

    # Per-path capability matcher for the hub's /worker/{id}/... REST routes.
    # rest.py intentionally carries no built-in authz (see its module docstring);
    # the protecting layer is us.  Without this, any authenticated principal —
    # including a viewer-role share token — could POST /worker/{id}/hijack/acquire
    # and seize control of a session.
    _HUB_WRITE_PATH = re.compile(
        r"^/worker/(?P<session_id>[\w\-]+)/"
        r"(?:hijack/(?:acquire|[\w\-]+/(?:send|step|heartbeat|release)))$"
    )
    _HUB_MODE_PATH = re.compile(r"^/worker/(?P<session_id>[\w\-]+)/input_mode$")
    _HUB_ADMIN_PATH = re.compile(r"^/worker/(?P<session_id>[\w\-]+)/disconnect_worker$")
    _HUB_READ_PATH = re.compile(r"^/worker/(?P<session_id>[\w\-]+)/hijack/[\w\-]+/(?:snapshot|events)$")

    async def _require_hub_route_authz(connection: HTTPConnection) -> None:
        """Gate /worker/{id}/... REST routes on session-level capabilities.

        Runs after _require_authenticated, so connection.state.uterm_principal
        is populated.  Applies only to REST paths served by the hub router;
        WebSocket routes handle their own per-session role resolution via
        _resolve_browser_role, so this dependency is a no-op for them.
        Uses HTTPConnection so the same dependency works for both the REST
        Request and WebSocket code paths FastAPI invokes.
        """
        path = str(connection.scope.get("path", ""))
        session_id: str | None = None
        required: str | None = None
        require_admin = False
        for pattern, cap in (
            (_HUB_WRITE_PATH, "session.control.hijack"),
            (_HUB_MODE_PATH, "session.control.mode"),
            (_HUB_READ_PATH, "session.read"),
        ):
            m = pattern.match(path)
            if m is not None:
                session_id = m.group("session_id")
                required = cap
                break
        if session_id is None:
            m = _HUB_ADMIN_PATH.match(path)
            if m is not None:
                session_id = m.group("session_id")
                require_admin = True
        if session_id is None:
            return  # Not a capability-gated hub route.
        principal = getattr(connection.state, "uterm_principal", None)
        if principal is None:  # pragma: no cover — _require_authenticated always sets this first
            raise HTTPException(status_code=401, detail="authentication required")
        authz_service = cast("AuthorizationService", connection.app.state.uterm_authz)
        if require_admin:
            if not await authz_service.is_admin(principal):
                raise HTTPException(status_code=403, detail="admin role required")
            return
        assert required is not None
        session = await registry.get_definition(session_id) if registry is not None else None
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        if required == "session.read":
            if not await authz_service.can_read_session(principal, session):
                raise HTTPException(status_code=403, detail="insufficient privileges")
        else:
            if not await authz_service.can_mutate_session(principal, session, required):
                raise HTTPException(status_code=403, detail="insufficient privileges")

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
            principal = await resolve_ws_principal(ws, config.auth)
        session = await registry.get_definition(worker_id) if registry is not None else None
        if session is None:
            return "admin" if config.auth.mode in {"none", "dev"} else "viewer"
        if not await authz.can_read_session(principal, session):
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="insufficient privileges")
        return await policy.role_for(principal, session)

    from provide.terminal.bridge.hub.ext import (
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
    if config.auth.identity_provider == "webhook" and config.auth.webhook_idp_url:
        idp = WebhookIdentityProvider(
            url=config.auth.webhook_idp_url,
            secret=config.auth.webhook_idp_secret,
            timeout_s=config.auth.webhook_idp_timeout_s,
        )
    else:
        idp = LocalIdentityProvider(config.auth)

    behavioral_audit_gate = None
    if config.governance.behavioral_audit_url:
        behavioral_audit_gate = WebhookBehavioralAuditGate(
            url=config.governance.behavioral_audit_url,
            secret=config.governance.behavioral_audit_secret,
        )
    behavioral_thresholds = BehavioralThresholds(
        max_cps=config.governance.behavioral_max_cps,
        min_jitter=config.governance.behavioral_min_jitter,
    )

    control_plane = _build_control_plane(config)
    resume_store = ControlPlaneResumeStore(control_plane)

    _hub_class = hub_class if hub_class is not None else TermHub
    hub = _hub_class(
        resolve_browser_role=_resolve_browser_role,
        on_metric=_inc_metric,
        worker_token=config.auth.worker_bearer_token,
        resume_store=resume_store,
        on_resume=_on_resume,
        browser_rate_limit_per_sec=config.browser_rate_limit_per_sec,
        event_bus=EventBus(),
        policy_gate=policy_gate,
        identity_provider=idp,
        delegate_roles=getattr(config.auth, "delegate_roles", True),
        behavioral_audit_gate=behavioral_audit_gate,
        behavioral_thresholds=behavioral_thresholds,
        behavioral_audit_interval_s=config.governance.behavioral_audit_interval_s,
    )
    # Attach the fan-out controller so routes and WS dispatch can find it.
    from provide.terminal.bridge.fanout import FanOutController, InMemoryFanOutStore

    hub.fan_out_controller = FanOutController(hub=hub, store=InMemoryFanOutStore())  # type: ignore[attr-defined]
    webhook_manager = WebhookManager()
    # Annotation detector scans snapshot/send text for security-relevant patterns.
    from provide.terminal.bridge.annotation._detector import PatternDetector

    from provide.terminal.server.discovery import (
        NoOpDiscoveryProvider,
        NodeStatus,
        WebhookDiscoveryProvider,
    )
    from provide.terminal.server.recording import LocalFileRecordingStore, WebhookRecordingStore

    # Choose Recording Store
    if config.recording.store_type == "webhook" and config.recording.webhook_url:
        recording_store = WebhookRecordingStore(
            url=config.recording.webhook_url,
            secret=config.recording.webhook_secret,
            timeout_s=config.recording.webhook_timeout_s,
        )
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

    async def _sweep_expired_tunnel_tokens() -> None:
        """Periodically remove expired tunnel tokens from the in-memory map."""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            expired = [
                sid
                for sid, state in tunnel_tokens.items()
                if isinstance(state.get("expires_at"), (int, float)) and now > float(state["expires_at"])
            ]
            for sid in expired:
                tunnel_tokens.pop(sid, None)
                logger.info("tunnel_token_expired session_id=%s swept=true", sid)

    async def _node_registry_heartbeat() -> None:
        """Periodically announce Node status to the External Management Tier."""
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
        idle_sweep_task = asyncio.create_task(_sweep_idle_sessions())
        retention_sweep_task = asyncio.create_task(_sweep_expired_sessions())
        heartbeat_task = asyncio.create_task(_node_registry_heartbeat())
        pam_task: asyncio.Task[None] | None = None
        with contextlib.suppress(ImportError):
            from provide.terminal.server.pam_integration import run_pam_integration

            pam_task = asyncio.create_task(run_pam_integration(config, registry))
        try:
            yield
        finally:
            if pam_task is not None:
                pam_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pam_task
            heartbeat_task.cancel()
            retention_sweep_task.cancel()
            idle_sweep_task.cancel()
            sweep_task.cancel()
            boot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            with contextlib.suppress(asyncio.CancelledError):
                await boot_task
            with contextlib.suppress(asyncio.CancelledError):
                await sweep_task
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
    app.state.uterm_metrics = metrics
    app.state.uterm_webhooks = webhook_manager
    app.state.uterm_profile_store = profile_store
    app.state.uterm_control_plane = control_plane
    app.state.uterm_tunnel_tokens = tunnel_tokens
    api_key_store = ApiKeyStore()
    app.state.uterm_api_key_store = api_key_store

    @app.middleware("http")
    async def _request_logging_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.uterm_request_id = request_id
        start = time.perf_counter()
        _inc_metric("http_requests_total")
        try:
            response = await call_next(request)
        except Exception:
            _inc_metric("http_requests_error_total")
            logger.exception(
                "http_request_failed request_id=%s method=%s path=%s",
                request_id,
                request.method,
                request.url.path,
            )
            raise
        duration_ms = (time.perf_counter() - start) * 1000.0
        if response.status_code >= 500:
            _inc_metric("http_requests_5xx_total")
        elif response.status_code >= 400:
            _inc_metric("http_requests_4xx_total")
        response.headers["x-request-id"] = request_id
        logger.info(
            "http_request request_id=%s method=%s path=%s status=%d duration_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response

    # Tunnel routes are passed as extra registrars to avoid a hard import
    # dependency from bridge → tunnel (enables future package extraction).
    from provide.terminal.bridge.fanout._routes import register_fanout_routes
    from provide.terminal.tunnel.fastapi_routes import register_tunnel_routes

    app.include_router(
        hub.create_router(extra_route_registrars=[register_tunnel_routes, register_fanout_routes]),
        dependencies=[Depends(_require_authenticated), Depends(_require_hub_route_authz)],
    )
    app.include_router(create_api_router(), dependencies=[Depends(_require_authenticated)])
    app.include_router(create_profiles_router(), dependencies=[Depends(_require_authenticated)])
    app.include_router(create_approvals_router(), dependencies=[Depends(_require_authenticated)])
    app.include_router(create_page_router(), prefix=config.ui.app_path, dependencies=[Depends(_require_authenticated)])

    @app.get("/s/{session_id}")
    async def short_share_url(request: FastAPIRequest, session_id: str) -> object:
        """Short share URL: /s/{id}?token=... → redirect to /app/{inspect|session}/{id}?token=..."""
        from starlette.responses import RedirectResponse

        tunnel_tokens: dict[str, dict[str, object]] = request.app.state.uterm_tunnel_tokens
        entry = tunnel_tokens.get(session_id, {})
        page = str(entry.get("share_page", "session"))
        qs = str(request.url.query)
        target = f"{config.ui.app_path}/{page}/{session_id}"
        if qs:
            target += f"?{qs}"
        return RedirectResponse(url=target, status_code=302)

    if config.server.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )

    app.add_middleware(SecurityHeadersMiddleware, config=config.security)
    app.add_middleware(TelemetryMiddleware)

    frontend_path = importlib.resources.files("provide.terminal.server") / "frontend"
    app.mount(config.ui.assets_path, StaticFiles(directory=str(frontend_path), html=False), name="uterm-assets")
    return app
