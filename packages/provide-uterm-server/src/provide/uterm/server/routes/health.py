#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Mutation-enforced at killed==100 ([tool.mutmut]); bound suite: tests/server/test_routes_mutation_killing.py (router-endpoint extraction, mocked Request).
"""Unauthenticated health-check endpoints for the hosted terminal server.

``/api/health`` returns rich diagnostic info (version, uptime, session count,
control-plane backend) and the optional features this server supports.  ``/healthz`` is a minimal liveness probe suitable for
Kubernetes readiness/liveness checks.

The health/liveness/readiness endpoints are mounted **without** authentication
dependencies so that external load balancers and orchestrators can reach them
unconditionally.  ``/api/security-posture`` is the exception: it reveals the
effective security config, so it is gated behind the supplied
``require_authenticated`` dependency (mirroring how the ``/api/metrics``
operational endpoint is gated) and is registered only when that dependency is
provided.

L10: the full posture report (the ``dev_opt_outs`` + ``warnings``
reconnaissance map of the deployment's security weaknesses) is restricted to
operator/admin principals. A lower-privileged authenticated principal (e.g. a
viewer) receives only a coarse ``{"environment", "secure"}`` summary. The
health router carries no operator-level dependency (only
``require_authenticated``), so the role split is enforced inside the endpoint
using the per-request ``uterm_principal`` (set by ``require_authenticated``)
and the app's ``uterm_authz`` service — rather than re-plumbing an
operator dependency through the router signature.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request, Response

from provide.uterm import __version__
from provide.uterm.server.app.posture import compute_security_posture
from provide.uterm.server.config_schema import OWNER_AND_GROUP_CAPTURE_SOCKET_MODE

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import HTTPConnection

# Optional behaviour a client cannot otherwise tell apart from an older server,
# listed in a ready ``/api/health`` answer so it can ask before relying on it.
# ``viewers.revoke.subject``: ``POST /api/sessions/{id}/viewers/revoke`` honours
# a ``{"subject": ...}`` body. A server without it ignores the body and revokes
# every share viewer of the session, so a caller must not send one blind.
SERVER_FEATURES = ("viewers.revoke.subject",)

# Listed only when configured, since a server can have either switched off:
# ``sessions.capture.persist``: PAM capture sessions are re-created under their
# ids after a restart (``session_persistence.persist_capture``).
# ``sessions.previous``: an ended capture session can be read at
# ``GET /api/previous-sessions`` (``session_persistence.previous_keep_days``).
# ``sessions.previous.owner``: an admin can set an ended capture session's
# owner and visibility (``PATCH /api/previous-sessions/{id}``), listed with
# ``sessions.previous`` since it has nothing to change without it.
PERSIST_CAPTURE_FEATURE = "sessions.capture.persist"
PREVIOUS_SESSIONS_FEATURE = "sessions.previous"
PREVIOUS_OWNER_FEATURE = "sessions.previous.owner"
# ``capture.socket_mode``: the sockets this server binds for pty_capture
# sessions let the socket directory's group connect, not only this server's
# user (``pam.capture_socket_mode = 0o660``). Listed only then, so a capture
# producer running as another user can tell that it will be able to connect.
# Other sockets are not covered: a pty session's injected capture socket stays
# owner-only in its own private directory.
CAPTURE_SOCKET_MODE_FEATURE = "capture.socket_mode"


def server_features(config: object) -> list[str]:
    """The features this server lists in ``/api/health``, given its config."""
    features = list(SERVER_FEATURES)
    settings = getattr(config, "session_persistence", None)
    if settings is not None and settings.persist_capture:
        features.append(PERSIST_CAPTURE_FEATURE)
    if settings is not None and settings.previous_keep_days > 0:
        features.extend((PREVIOUS_SESSIONS_FEATURE, PREVIOUS_OWNER_FEATURE))
    pam = getattr(config, "pam", None)
    if pam is not None and pam.capture_socket_mode == OWNER_AND_GROUP_CAPTURE_SOCKET_MODE:
        features.append(CAPTURE_SOCKET_MODE_FEATURE)
    return features


def create_health_router(
    require_authenticated: Callable[[HTTPConnection], Awaitable[None]] | None = None,
) -> APIRouter:
    """Return a router with ``/api/health``, ``/healthz``, and ``/readyz``.

    When ``require_authenticated`` is supplied, the router also exposes an
    auth-gated ``GET /api/security-posture`` endpoint that returns
    ``compute_security_posture(app.state.uterm_config)``.  The dependency is
    required (the endpoint must never be anonymous-public) so the route is
    omitted entirely when no dependency is provided.
    """
    router = APIRouter()

    @router.get("/api/health")
    async def health(request: Request, response: Response) -> dict[str, object]:
        """Rich health check with version, uptime, sessions, and backend info."""
        registry = getattr(request.app.state, "uterm_registry", None)
        if registry is None:
            response.status_code = 503
            return {"status": "unavailable", "ok": False, "ready": False, "service": "uterm-server"}

        # Gate on the readiness flag — set only after migrate() + background tasks
        # succeed. A pod with registry attached but lifespan not yet finished
        # (or migrate() failed) stays 503 here even though registry is non-None.
        uterm_ready: bool = getattr(request.app.state, "uterm_ready", False)
        if not uterm_ready:
            response.status_code = 503
            return {"status": "starting", "ok": False, "ready": False, "service": "uterm-server"}

        startup_time: float = getattr(request.app.state, "uterm_startup_time", 0.0)
        uptime_s = round(time.time() - startup_time, 2) if startup_time > 0 else 0.0

        config = getattr(request.app.state, "uterm_config", None)
        backend = "memory"
        if config is not None:
            backend = str(getattr(getattr(config, "control_plane", None), "backend", "memory"))

        # Count sessions from the registry's internal dict — fast, no async lock
        # needed for a simple len() on a dict (GIL-safe snapshot).
        sessions_dict = getattr(registry, "_sessions", None)
        active_sessions = len(sessions_dict) if isinstance(sessions_dict, dict) else 0

        return {
            "status": "ok",
            "ok": True,
            "ready": True,
            "service": "uterm-server",
            "version": __version__,
            "uptime_s": uptime_s,
            "active_sessions": active_sessions,
            "control_plane_backend": backend,
            "features": server_features(config),
        }

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Minimal liveness probe for Kubernetes — no dependencies, always 200."""
        return {"status": "ok"}

    @router.get("/readyz")
    async def readyz(request: Request, response: Response) -> dict[str, str]:
        """Readiness probe — 200 only after lifespan startup completes fully.

        Returns 503 until ``app.state.uterm_ready`` is set True (which happens
        after ``control_plane.migrate()`` succeeds and all background tasks are
        created).  Use this for Kubernetes readinessProbe; use ``/healthz`` for
        livenessProbe.
        """
        if getattr(request.app.state, "uterm_ready", False):
            return {"status": "ready"}
        response.status_code = 503
        return {"status": "not_ready"}

    if require_authenticated is not None:

        @router.get("/api/security-posture", dependencies=[Depends(require_authenticated)])
        async def security_posture(request: Request) -> dict[str, object]:
            """Auth-gated effective-security-posture self-report.

            Reveals the active dev opt-outs and ``secure`` summary, so it is
            never anonymous-public — the route exists only because a
            ``require_authenticated`` dependency was supplied at wiring time.

            L10: the full report (``dev_opt_outs`` + ``warnings`` — a
            reconnaissance map of the deployment's security weaknesses) is only
            returned to an operator/admin principal. A lower-privileged
            authenticated principal (e.g. a viewer) receives only a coarse
            ``{"environment", "secure"}`` summary, so it cannot enumerate the
            deployment's relaxed controls.
            """
            config = request.app.state.uterm_config
            posture = compute_security_posture(config)
            if await _posture_caller_is_privileged(request):
                return posture
            # Coarse summary for a non-operator authenticated principal.
            return {"environment": posture["environment"], "secure": posture["secure"]}

        async def _posture_caller_is_privileged(request: Request) -> bool:
            """True iff the per-request principal is an operator or admin.

            ``require_authenticated`` has already stored the resolved principal
            on ``request.state``. Authorization is delegated to the app's
            ``uterm_authz`` service (operator role or global admin), mirroring
            the role checks the operator-only ``/api/approvals`` router uses.
            """
            principal = getattr(request.state, "uterm_principal", None)
            authz = getattr(request.app.state, "uterm_authz", None)
            if principal is None or authz is None:
                return False
            return bool(await authz.is_admin(principal) or await authz.has_role(principal, "operator"))

    return router
