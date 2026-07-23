#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Mutation-enforced at killed==100 ([tool.mutmut]); bound suite: tests/server/test_routes_mutation_killing.py (router-endpoint extraction, mocked Request).
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
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Body, HTTPException, Request

from provide.telemetry import get_tracer
from provide.uterm.api_routes import API_ROUTES, RouteDef
from provide.uterm.server.audit import audit_event
from provide.uterm.server.egress import EgressBlockedError, assert_session_egress_allowed
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
from provide.uterm.server.routes.route_defs import bind_api_routes
from provide.uterm.server.tunnel_invites import discard_tunnel_invites_for_session, issue_tunnel_invites
from provide.uterm.tunnel.token_hash import hash_token

# Finding #12: keys masked before connector_config is persisted on the session
# record / written to audit logs.  These match the connector schemas that
# carry secrets (telnet/ssh password, ssh passphrase, generic ``token`` and
# ``secret`` fields).  The connector still receives the plaintext via a
# transient variable; only the persisted record is scrubbed.
_SENSITIVE_CONFIG_KEYS = frozenset({"password", "passphrase", "secret", "token"})
_SCRUB_SENTINEL = "***"  # sentinel placeholder, not a credential

if TYPE_CHECKING:
    from collections.abc import Callable


def _scrub_sensitive(config: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of *config* with sensitive values masked.

    Used before passing connector config into ``registry.create_session`` and
    before any audit-log emission so plaintext credentials never end up in the
    persisted session record or the audit trail.  The masking is a sentinel
    (``"***"``) rather than removal so downstream consumers can still see that
    a credential WAS supplied (useful for shape-checking in the UI).
    """
    return {k: (_SCRUB_SENTINEL if k in _SENSITIVE_CONFIG_KEYS else v) for k, v in config.items()}


def tunnel_capability_handlers() -> dict[str, Callable[..., object]]:
    """Return the FastAPI handlers for shared tunnel RouteDefs."""

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
        # Finding #12: scrub password/passphrase/secret/token BEFORE the config
        # is persisted onto the session record or any audit log.  Previously
        # the plaintext password flowed through ``registry.create_session``
        # into the session definition, then leaked back out via the response
        # body (``model_dump(session)``) and the in-process session listing
        # endpoint.  See ``_scrub_sensitive`` at module top.
        scrubbed_config = _scrub_sensitive(connector_config)
        session_payload: dict[str, Any] = {
            "session_id": session_id,
            "display_name": display_name,
            "connector_type": connector_type,
            "connector_config": scrubbed_config,
            "input_mode": input_mode,
            "tags": tags,
            "auto_start": True,
            "ephemeral": True,
            "visibility": "private",
            "owner": p.subject_id,
        }
        if payload.get("recording_enabled"):
            session_payload["recording_enabled"] = True
        # Egress guard: block cloud-metadata IPs always; optionally block private
        # targets (multi-tenant posture).  Host derivation is single-sourced in
        # assert_session_egress_allowed (also enforced at the registry
        # chokepoint, so this is defense-in-depth that yields a synchronous 422).
        # The internal tunnel path (create_tunnel) uses connector_type="websocket"
        # with no url in the payload, so it is naturally skipped (no host derived).
        _cfg = request.app.state.uterm_config
        _block_private: bool = _cfg.security.block_private_connector_targets
        try:
            await assert_session_egress_allowed(connector_type, connector_config, block_private=_block_private)
        except EgressBlockedError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
            # Connector_type only — never include the raw payload here, even
            # masked.  Audit downstreams may persist this verbatim.
            detail={"connector_type": connector_type, "ephemeral": True},
        )
        cfg = request.app.state.uterm_config
        url = f"{cfg.ui.app_path}/session/{session_id}"
        return {"session_id": session_id, "url": url, **model_dump(session)}

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

        src_ip = source_ip(request)

        reg = registry(request)
        base = cfg.server.public_base_url or str(request.base_url).rstrip("/")
        ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
        tunnel_tokens = cast("dict[str, dict[str, object]]", request.app.state.uterm_tunnel_tokens)
        tunnel_invites = cast("dict[str, dict[str, object]]", request.app.state.uterm_tunnel_invites)

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
                        # Tunnels rely on one-time invite URLs that bootstrap an
                        # HttpOnly tunnel cookie. The session itself is private
                        # and owned by the creator so other authenticated
                        # principals cannot list/read/mutate it without that
                        # cookie.
                        "owner": p.subject_id,
                        "visibility": "private",
                        "recording_enabled": True,
                    },
                    validate_connector_target=False,
                )
            except SessionValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        # Mirrored in cloudflare/api/_tunnel_api.py -- keep both in sync.
        # The hub holds BLAKE2b digests, not the raw tokens — a memory
        # disclosure on the server can no longer leak the bearer values.
        # The plain tokens leave this scope only via the JSON response
        # below and never again return to the hub's in-process state.
        share_page = "inspect" if tunnel_type == "http" else "session"
        tunnel_tokens[tunnel_id] = {
            "worker_token_hash": hash_token(worker_token),
            "share_token_hash": hash_token(share_token),
            "control_token_hash": hash_token(control_token),
            "created_at": now,
            "expires_at": expires_at,
            "issued_ip": src_ip if tunnel_cfg.ip_binding else None,
            "tunnel_type": tunnel_type,
            "share_page": share_page,
        }
        share_invite, control_invite = issue_tunnel_invites(
            tunnel_invites,
            session_id=tunnel_id,
            share_token=share_token,
            control_token=control_token,
            tunnel_expires_at=expires_at,
            issued_ip=src_ip if tunnel_cfg.ip_binding else None,
            now=now,
        )

        from provide.telemetry import get_logger

        get_logger(__name__).info(
            "tunnel_token_created session_id=%s ttl_s=%d source_ip=%s",
            tunnel_id,
            ttl_s,
            src_ip,
        )
        audit_event(
            "tunnel.create",
            principal=p.subject_id,
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
            "share_url": f"{base}/s/{tunnel_id}?invite={share_invite}",
            "control_url": f"{base}/s/{tunnel_id}?invite={control_invite}",
            "expires_at": expires_at,
        }

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
        tunnel_invites = cast("dict[str, dict[str, object]]", request.app.state.uterm_tunnel_invites)
        discard_tunnel_invites_for_session(tunnel_invites, tunnel_id)
        from provide.telemetry import get_logger

        get_logger(__name__).info("tunnel_token_revoked session_id=%s found=%s", tunnel_id, removed is not None)
        audit_event(
            "tunnel.tokens.revoke",
            principal=p.subject_id,
            session_id=tunnel_id,
            source_ip=source_ip(request),
        )
        return {"ok": True, "session_id": tunnel_id}

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
        tunnel_invites = cast("dict[str, dict[str, object]]", request.app.state.uterm_tunnel_invites)
        old = tunnel_tokens.get(tunnel_id)
        if old is None:
            raise HTTPException(status_code=404, detail=f"no tunnel tokens for {tunnel_id}")

        cfg = request.app.state.uterm_config
        ttl_s = cfg.tunnel.token_ttl_s
        now = time.time()
        worker_token = secrets.token_urlsafe(32)
        share_token = secrets.token_urlsafe(32)
        control_token = secrets.token_urlsafe(32)
        src_ip = source_ip(request)

        tunnel_type_r = str(old.get("tunnel_type", "terminal"))
        share_page_r = "inspect" if tunnel_type_r == "http" else "session"
        tunnel_tokens[tunnel_id] = {
            "worker_token_hash": hash_token(worker_token),
            "share_token_hash": hash_token(share_token),
            "control_token_hash": hash_token(control_token),
            "created_at": now,
            "expires_at": now + ttl_s,
            "issued_ip": src_ip if cfg.tunnel.ip_binding else None,
            "tunnel_type": tunnel_type_r,
            "share_page": share_page_r,
        }
        discard_tunnel_invites_for_session(tunnel_invites, tunnel_id)
        share_invite, control_invite = issue_tunnel_invites(
            tunnel_invites,
            session_id=tunnel_id,
            share_token=share_token,
            control_token=control_token,
            tunnel_expires_at=now + ttl_s,
            issued_ip=src_ip if cfg.tunnel.ip_binding else None,
            now=now,
        )

        base = cfg.server.public_base_url or str(request.base_url).rstrip("/")
        ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
        from provide.telemetry import get_logger

        get_logger(__name__).info("tunnel_token_rotated session_id=%s source_ip=%s", tunnel_id, src_ip)
        audit_event(
            "tunnel.tokens.rotate",
            principal=p.subject_id,
            session_id=tunnel_id,
            source_ip=src_ip,
        )

        return {
            "tunnel_id": tunnel_id,
            "ws_endpoint": f"{ws_base}/tunnel/{tunnel_id}",
            "worker_token": worker_token,
            "share_url": f"{base}/s/{tunnel_id}?invite={share_invite}",
            "control_url": f"{base}/s/{tunnel_id}?invite={control_invite}",
            "expires_at": now + ttl_s,
        }

    return {
        "tunnels.connect": quick_connect,
        "tunnels.create": create_tunnel,
        "tunnels.revoke_token": revoke_tunnel_tokens,
        "tunnels.rotate_token": rotate_tunnel_tokens,
    }


async def _unregistered_capability_handler() -> None:
    """Satisfy the adapter's complete-inventory validation for unbound routes."""
    raise RuntimeError("unregistered shared API capability invoked")


def register_tunnel_routes(router: APIRouter) -> None:
    """Bind the shared tunnel HTTP family exactly once through RouteDefs."""
    tunnel_handlers = tunnel_capability_handlers()
    handlers: dict[str, Callable[..., object]] = {
        route.capability: _unregistered_capability_handler for route in API_ROUTES
    }
    handlers.update(tunnel_handlers)
    selected: tuple[RouteDef, ...] = tuple(route for route in API_ROUTES if route.capability in tunnel_handlers)
    tunnel_router = APIRouter()
    bind_api_routes(tunnel_router, handlers, selected)
    router.routes.extend(tunnel_router.routes)
