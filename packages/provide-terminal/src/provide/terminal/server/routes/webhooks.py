#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Webhook CRUD routes for the hosted server app.

Exposes:
  POST   /api/sessions/{session_id}/webhooks          — register
  GET    /api/sessions/{session_id}/webhooks          — list
  DELETE /api/sessions/{session_id}/webhooks/{wh_id} — unregister
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Body, HTTPException, Path, Request

if TYPE_CHECKING:
    from provide.terminal.server.auth import Principal
    from provide.terminal.server.authorization import AuthorizationService
    from provide.terminal.server.registry import SessionRegistry
    from provide.terminal.server.webhooks import WebhookManager

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


def create_webhook_router() -> APIRouter:
    router = APIRouter()

    @router.post("/sessions/{session_id}/webhooks")
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
        if not authz.can_read_session(principal, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")

        url = payload.get("url")
        if not url or not isinstance(url, str):
            raise HTTPException(status_code=422, detail="url is required")

        event_types = payload.get("event_types")
        if event_types is not None and not isinstance(event_types, list):
            raise HTTPException(status_code=422, detail="event_types must be a list")

        pattern = payload.get("pattern")
        secret = payload.get("secret")

        manager = _webhook_manager(request)
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

    @router.get("/sessions/{session_id}/webhooks")
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
        if not authz.can_read_session(principal, definition):
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

    @router.delete("/sessions/{session_id}/webhooks/{webhook_id}")
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
        if not authz.can_read_session(principal, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")

        manager = _webhook_manager(request)
        # Verify webhook belongs to this session before unregistering.
        cfg = manager.get_webhook(webhook_id)
        if cfg is None or cfg.session_id != session_id:
            raise HTTPException(status_code=404, detail=f"unknown webhook: {webhook_id}")

        await manager.unregister(webhook_id)
        return {"ok": True, "webhook_id": webhook_id}

    return router
