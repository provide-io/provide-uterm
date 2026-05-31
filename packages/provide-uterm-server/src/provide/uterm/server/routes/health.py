#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unauthenticated health-check endpoints for the hosted terminal server.

``/api/health`` returns rich diagnostic info (version, uptime, session count,
control-plane backend).  ``/healthz`` is a minimal liveness probe suitable for
Kubernetes readiness/liveness checks.

Both endpoints are mounted **without** authentication dependencies so that
external load balancers and orchestrators can reach them unconditionally.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request, Response

from provide.uterm import __version__


def create_health_router() -> APIRouter:
    """Return a router with ``/api/health``, ``/healthz``, and ``/readyz``."""
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

    return router
