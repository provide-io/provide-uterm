#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Mutation-enforced at killed==100 (281/281, no documented equivalents). Bound
# suites: tests/server/test_routes_mutation_killing.py (decorated-era surface)
# and tests/server/test_routes_webhooks_mutation_killing.py (the handlers).
#
# Measured 89.32% until 2026-08-10. A webhook makes the server issue outbound
# HTTP on session activity, so this is an SSRF surface as much as a CRUD one:
# the URL must go through manager.validate_url, the NORMALISED return value is
# what gets registered (never the raw payload), and unregister verifies the
# webhook belongs to THIS session -- without which any principal who may edit
# their own session could delete another session's webhook by id.
"""Webhook CRUD routes for the hosted server app.

Exposes:
  POST   /api/sessions/{session_id}/webhooks          — register
  GET    /api/sessions/{session_id}/webhooks          — list
  DELETE /api/sessions/{session_id}/webhooks/{wh_id} — unregister
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Body, HTTPException, Path, Request

from provide.uterm.api_routes import API_ROUTES, RouteDef
from provide.uterm.server.routes.route_defs import bind_api_routes

if TYPE_CHECKING:
    from collections.abc import Callable

    from provide.uterm.server.auth import Principal
    from provide.uterm.server.authorization import AuthorizationService
    from provide.uterm.server.registry import SessionRegistry
    from provide.uterm.server.webhooks import WebhookManager

# Validated path parameters — rejects path-unsafe characters.
_SessionId = Annotated[str, Path(pattern=r"^[\w\-]+$")]
_WebhookId = Annotated[str, Path(pattern=r"^[\w\-]+$")]


def _registry(request: Request) -> SessionRegistry:
    return cast("SessionRegistry", request.app.state.uterm_registry)


def _authz(request: Request) -> AuthorizationService:
    return cast("AuthorizationService", request.app.state.uterm_authz)


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "uterm_principal", None)
    if principal is None:  # pragma: no cover — middleware always sets this
        raise HTTPException(status_code=500, detail="principal was not resolved")
    return cast("Principal", principal)


def _webhook_manager(request: Request) -> WebhookManager:
    mgr = getattr(request.app.state, "uterm_webhooks", None)
    if mgr is None:  # pragma: no cover — lifespan always sets this
        raise HTTPException(status_code=503, detail="webhook manager not available")
    return cast("WebhookManager", mgr)


def webhook_capability_handlers() -> dict[str, Callable[..., object]]:
    """Return the FastAPI handlers for shared webhook RouteDefs."""

    async def register_webhook(
        request: Request,
        session_id: _SessionId,
        payload: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        """Register a webhook for the session.

        Body fields:
            url (str): URL to POST events to.
            event_types (list[str], optional): Filter to specific event types.
            pattern (str, optional): Regex filter on snapshot screen text.
            secret (str, optional): HMAC-SHA256 signing key.
        """
        principal = _principal(request)
        authz = _authz(request)
        registry = _registry(request)

        definition = await registry.get_definition(session_id)
        if definition is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        if not await authz.can_mutate_session(principal, definition, "session.control.update"):
            raise HTTPException(status_code=403, detail="insufficient privileges")

        url = payload.get("url")
        if not url or not isinstance(url, str):
            raise HTTPException(status_code=422, detail="url is required")
        manager = _webhook_manager(request)
        try:
            url = manager.validate_url(url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        event_types = payload.get("event_types")
        if event_types is not None and not isinstance(event_types, list):
            raise HTTPException(status_code=422, detail="event_types must be a list")

        pattern = payload.get("pattern")
        try:
            pattern = manager.validate_pattern(pattern)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        secret = payload.get("secret")

        event_bus = getattr(request.app.state.uterm_hub, "event_bus", None)
        cfg = await manager.register(
            session_id,
            url,
            event_types=event_types,
            pattern=pattern,
            secret=secret,
            event_bus=event_bus,
        )
        return {
            "webhook_id": cfg.webhook_id,
            "session_id": cfg.session_id,
            "url": cfg.url,
            "event_types": list(cfg.event_types) if cfg.event_types is not None else None,
            "pattern": cfg.pattern,
        }

    async def list_webhooks(
        request: Request,
        session_id: _SessionId,
    ) -> dict[str, Any]:
        """List all registered webhooks for the session."""
        principal = _principal(request)
        authz = _authz(request)
        registry = _registry(request)

        definition = await registry.get_definition(session_id)
        if definition is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        if not await authz.can_mutate_session(principal, definition, "session.control.update"):
            raise HTTPException(status_code=403, detail="insufficient privileges")

        manager = _webhook_manager(request)
        webhooks = manager.list_webhooks(session_id)
        return {
            "webhooks": [
                {
                    "webhook_id": cfg.webhook_id,
                    "session_id": cfg.session_id,
                    "url": cfg.url,
                    "event_types": list(cfg.event_types) if cfg.event_types is not None else None,
                    "pattern": cfg.pattern,
                }
                for cfg in webhooks
            ]
        }

    async def unregister_webhook(
        request: Request,
        session_id: _SessionId,
        webhook_id: _WebhookId,
    ) -> dict[str, Any]:
        """Unregister a webhook by ID."""
        principal = _principal(request)
        authz = _authz(request)
        registry = _registry(request)

        definition = await registry.get_definition(session_id)
        if definition is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        if not await authz.can_mutate_session(principal, definition, "session.control.update"):
            raise HTTPException(status_code=403, detail="insufficient privileges")

        manager = _webhook_manager(request)
        # Verify webhook belongs to this session before unregistering.
        cfg = manager.get_webhook(webhook_id)
        if cfg is None or cfg.session_id != session_id:
            raise HTTPException(status_code=404, detail=f"unknown webhook: {webhook_id}")

        await manager.unregister(webhook_id)
        return {"ok": True, "webhook_id": webhook_id}

    return {
        "sessions.webhooks.create": register_webhook,
        "sessions.webhooks.list": list_webhooks,
        "sessions.webhooks.delete": unregister_webhook,
    }


async def _unregistered_capability_handler() -> None:
    """Satisfy the adapter's complete-inventory validation for unbound routes."""
    raise RuntimeError("unregistered shared API capability invoked")


def register_webhook_routes(router: APIRouter) -> None:
    """Bind the shared webhook HTTP family exactly once through RouteDefs."""
    webhook_handlers = webhook_capability_handlers()
    handlers: dict[str, Callable[..., object]] = {
        route.capability: _unregistered_capability_handler for route in API_ROUTES
    }
    handlers.update(webhook_handlers)
    selected: tuple[RouteDef, ...] = tuple(route for route in API_ROUTES if route.capability in webhook_handlers)
    webhook_router = APIRouter()
    bind_api_routes(webhook_router, handlers, selected)
    router.routes.extend(webhook_router.routes)
