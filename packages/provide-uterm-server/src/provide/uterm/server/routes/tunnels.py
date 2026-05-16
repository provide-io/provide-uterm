#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Quick-connect and tunnel routes for the hosted server app.

Exposes:
  POST   /api/connect                          -- quick connect
  POST   /api/tunnels                          -- create tunnel
  DELETE /api/tunnels/{tunnel_id}/tokens        -- revoke tunnel tokens
  POST   /api/tunnels/{tunnel_id}/tokens/rotate -- rotate tunnel tokens
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Body, HTTPException, Request

from provide.telemetry import get_tracer
from provide.uterm.server.audit import audit_event
from provide.uterm.server.models import model_dump
from provide.uterm.server.registry import SessionValidationError
from provide.uterm.server.routes._helpers import (
    SessionId,
    authz,
    principal,
    registry,
    set_span_attrs,
    source_ip,
)


def create_tunnels_router() -> APIRouter:
    """Build a router for quick-connect and tunnel endpoints."""
    router = APIRouter()

    @router.post("/connect")
    async def quick_connect(request: Request, payload: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        p = principal(request)
        az = authz(request)
        if not await az.can_create_session(p):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        connector_type = str(payload.get("connector_type", "ssh")).strip()
        display_name = str(payload.get("display_name") or connector_type).strip() or connector_type
        input_mode = str(payload.get("input_mode", "open")).strip()
        tags_raw = payload.get("tags", [])
        tags = [str(t).strip() for t in tags_raw if str(t).strip()] if isinstance(tags_raw, list) else []
        session_id = f"connect-{uuid.uuid4().hex[:12]}"
        # Exclude session-level fields so they are not passed into connector_config,
        # which would cause connectors to reject them as unknown keys.
        _top_level = {
            "connector_type",
            "display_name",
            "input_mode",
            "tags",
            "auto_start",
            "visibility",
            "owner",
            "recording_enabled",
            "ephemeral",
        }
        connector_config = {k: v for k, v in payload.items() if k not in _top_level}
        session_payload: dict[str, Any] = {
            "session_id": session_id,
            "display_name": display_name,
            "connector_type": connector_type,
            "connector_config": connector_config,
            "input_mode": input_mode,
            "tags": tags,
            "auto_start": True,
            "ephemeral": True,
            "visibility": "private",
            "owner": p.subject_id,
        }
        if payload.get("recording_enabled"):
            session_payload["recording_enabled"] = True
        with get_tracer(__name__).start_as_current_span("uterm.session.quick_connect") as span:
            set_span_attrs(
                span,
                session_id=session_id,
                operation="session.quick_connect",
                principal=p.subject_id,
                http_method="POST",
                http_path="/api/connect",
            )
            try:
                session = await registry(request).create_session(session_payload)
            except SessionValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit_event(
            "session.create",
            principal=p.subject_id,
            session_id=session_id,
            source_ip=source_ip(request),
            detail={"connector_type": connector_type, "ephemeral": True},
        )
        cfg = request.app.state.uterm_config
        url = f"{cfg.ui.app_path}/session/{session_id}"
        return {"session_id": session_id, "url": url, **model_dump(session)}

    @router.post("/tunnels")
    async def create_tunnel(request: Request, payload: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        """Create a tunnel session for ``uterm share``.

        Returns tunnel_id, ws_endpoint, share_url, control_url, and expires_at.
        Accepts optional ``ttl_s`` in payload to override server default.
        """
        import secrets

        p = principal(request)
        az = authz(request)
        if not await az.can_create_session(p):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        cfg = request.app.state.uterm_config
        tunnel_cfg = cfg.tunnel

        tunnel_type = str(payload.get("tunnel_type", "terminal")).strip()
        display_name = str(payload.get("display_name") or "tunnel").strip()
        tunnel_id = f"tunnel-{uuid.uuid4().hex[:12]}"
        worker_token = secrets.token_urlsafe(32)
        share_token = secrets.token_urlsafe(32)
        control_token = secrets.token_urlsafe(32)

        # TTL: per-tunnel override clamped to [60, server default * 24]
        requested_ttl = int(payload.get("ttl_s", tunnel_cfg.token_ttl_s))
        max_ttl = tunnel_cfg.token_ttl_s * 24
        ttl_s = max(60, min(requested_ttl, max_ttl))
        now = time.time()
        expires_at = now + ttl_s

        src_ip = str(getattr(request.client, "host", "unknown")) if request.client else "unknown"

        reg = registry(request)
        base = cfg.server.public_base_url or str(request.base_url).rstrip("/")
        ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
        tunnel_tokens = cast("dict[str, dict[str, object]]", request.app.state.uterm_tunnel_tokens)

        with get_tracer(__name__).start_as_current_span("uterm.tunnel.create") as span:
            set_span_attrs(
                span,
                session_id=tunnel_id,
                operation="tunnel.create",
                principal=p.subject_id,
                http_method="POST",
                http_path="/api/tunnels",
            )
            try:
                await reg.create_session(
                    {
                        "session_id": tunnel_id,
                        "display_name": display_name,
                        "connector_type": "websocket",
                        "connector_config": {"tunnel_type": tunnel_type},
                        "input_mode": "open",
                        "auto_start": False,
                        "ephemeral": True,
                        # Tunnels rely on bearer-capability URLs (share_url /
                        # control_url).  The session itself is private and owned
                        # by the creator so other authenticated principals cannot
                        # list/read/mutate it without the token.
                        "owner": p.subject_id,
                        "visibility": "private",
                        "recording_enabled": True,
                    }
                )
            except SessionValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        # Mirrored in cloudflare/api/_tunnel_api.py -- keep both in sync.
        share_page = "inspect" if tunnel_type == "http" else "session"
        tunnel_tokens[tunnel_id] = {
            "worker_token": worker_token,
            "share_token": share_token,
            "control_token": control_token,
            "created_at": now,
            "expires_at": expires_at,
            "issued_ip": src_ip if tunnel_cfg.ip_binding else None,
            "tunnel_type": tunnel_type,
            "share_page": share_page,
        }

        from provide.telemetry import get_logger

        get_logger(__name__).info(
            "tunnel_token_created session_id=%s ttl_s=%d source_ip=%s",
            tunnel_id,
            ttl_s,
            src_ip,
        )
        audit_event(
            "tunnel.create",
            principal=principal(request).subject_id,
            session_id=tunnel_id,
            source_ip=src_ip,
            detail={"tunnel_type": tunnel_type, "ttl_s": ttl_s},
        )
        return {
            "tunnel_id": tunnel_id,
            "display_name": display_name,
            "tunnel_type": tunnel_type,
            "ws_endpoint": f"{ws_base}/tunnel/{tunnel_id}",
            "worker_token": worker_token,
            "share_url": f"{base}{cfg.ui.app_path}/{share_page}/{tunnel_id}?token={share_token}",
            "control_url": f"{base}{cfg.ui.app_path}/operator/{tunnel_id}?token={control_token}",
            "expires_at": expires_at,
        }

    @router.delete("/tunnels/{tunnel_id}/tokens")
    async def revoke_tunnel_tokens(request: Request, tunnel_id: SessionId) -> dict[str, Any]:
        """Revoke all tokens for a tunnel session. Owner or admin only.

        Idempotent: if the tunnel session no longer exists, return 200 (the
        tokens are effectively already revoked).  If the session exists but
        the caller is not the owner/admin, return 403.
        """
        p = principal(request)
        az = authz(request)
        session = await registry(request).get_definition(tunnel_id)
        if session is not None and not (await az.is_admin(p) or await az.is_owner(p, session)):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        tunnel_tokens = cast("dict[str, dict[str, object]]", request.app.state.uterm_tunnel_tokens)
        removed = tunnel_tokens.pop(tunnel_id, None)
        from provide.telemetry import get_logger

        get_logger(__name__).info("tunnel_token_revoked session_id=%s found=%s", tunnel_id, removed is not None)
        audit_event(
            "tunnel.tokens.revoke",
            principal=principal(request).subject_id,
            session_id=tunnel_id,
            source_ip=source_ip(request),
        )
        return {"ok": True, "session_id": tunnel_id}

    @router.post("/tunnels/{tunnel_id}/tokens/rotate")
    async def rotate_tunnel_tokens(request: Request, tunnel_id: SessionId) -> dict[str, Any]:
        """Rotate all tokens for a tunnel session. Owner or admin only."""
        import secrets

        p = principal(request)
        az = authz(request)
        session = await registry(request).get_definition(tunnel_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {tunnel_id}")
        if not (await az.is_admin(p) or await az.is_owner(p, session)):
            raise HTTPException(status_code=403, detail="insufficient privileges")

        tunnel_tokens = cast("dict[str, dict[str, object]]", request.app.state.uterm_tunnel_tokens)
        old = tunnel_tokens.get(tunnel_id)
        if old is None:
            raise HTTPException(status_code=404, detail=f"no tunnel tokens for {tunnel_id}")

        cfg = request.app.state.uterm_config
        ttl_s = cfg.tunnel.token_ttl_s
        now = time.time()
        worker_token = secrets.token_urlsafe(32)
        share_token = secrets.token_urlsafe(32)
        control_token = secrets.token_urlsafe(32)
        src_ip = str(getattr(request.client, "host", "unknown")) if request.client else "unknown"

        tunnel_type_r = str(old.get("tunnel_type", "terminal"))
        share_page_r = "inspect" if tunnel_type_r == "http" else "session"
        tunnel_tokens[tunnel_id] = {
            "worker_token": worker_token,
            "share_token": share_token,
            "control_token": control_token,
            "created_at": now,
            "expires_at": now + ttl_s,
            "issued_ip": src_ip if cfg.tunnel.ip_binding else None,
            "tunnel_type": tunnel_type_r,
            "share_page": share_page_r,
        }

        base = cfg.server.public_base_url or str(request.base_url).rstrip("/")
        ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
        from provide.telemetry import get_logger

        get_logger(__name__).info("tunnel_token_rotated session_id=%s source_ip=%s", tunnel_id, src_ip)
        audit_event(
            "tunnel.tokens.rotate",
            principal=principal(request).subject_id,
            session_id=tunnel_id,
            source_ip=src_ip,
        )

        return {
            "tunnel_id": tunnel_id,
            "ws_endpoint": f"{ws_base}/tunnel/{tunnel_id}",
            "worker_token": worker_token,
            "share_url": f"{base}{cfg.ui.app_path}/{share_page_r}/{tunnel_id}?token={share_token}",
            "control_url": f"{base}{cfg.ui.app_path}/operator/{tunnel_id}?token={control_token}",
            "expires_at": now + ttl_s,
        }

    return router
