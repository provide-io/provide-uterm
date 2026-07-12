#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Mutation-enforced at killed==100 ([tool.mutmut]); bound suite: tests/server/test_routes_mutation_killing.py (router-endpoint extraction, mocked Request).
"""API key management routes (admin-only)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Body, HTTPException, Path, Request

from provide.uterm.server.audit import audit_event

if TYPE_CHECKING:
    from provide.uterm.server.auth import Principal
    from provide.uterm.server.authorization import AuthorizationService

_ALLOWED_ROLE_SCOPES = frozenset({"viewer", "operator", "admin"})


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "uterm_principal", None)
    if principal is None:
        raise HTTPException(status_code=500, detail="principal was not resolved")
    return cast("Principal", principal)


def _authz(request: Request) -> AuthorizationService:
    return cast("AuthorizationService", request.app.state.uterm_authz)


def _source_ip(request: Request) -> str:
    return str(getattr(request.client, "host", "unknown")) if request.client else "unknown"


def _tenant_scope(principal: Principal) -> str:
    """Require a canonical non-empty tenant identity for public key management."""
    tenant_id = getattr(principal, "tenant_id", None)
    if not isinstance(tenant_id, str):
        raise HTTPException(status_code=403, detail="tenant identity required for API key management")
    return tenant_id


def create_api_keys_router() -> APIRouter:
    """Return a sub-router for ``/api/keys`` endpoints."""
    router = APIRouter()

    @router.post("/keys")
    async def create_api_key(
        request: Request,
        payload: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        if not await authz.is_admin(principal):
            raise HTTPException(status_code=403, detail="admin role required")
        tenant_id = _tenant_scope(principal)
        cfg = request.app.state.uterm_config
        if not cfg.auth.api_keys_enabled:
            raise HTTPException(status_code=403, detail="API key management is disabled")
        store = request.app.state.uterm_api_key_store
        name = str(payload.get("name", "")).strip()
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        if "scopes" not in payload:
            raise HTTPException(status_code=422, detail="scopes is required")
        scopes_raw = payload.get("scopes")
        if not isinstance(scopes_raw, list):
            raise HTTPException(status_code=422, detail="scopes must be a list of role scopes")
        scopes = frozenset(str(s).strip() for s in scopes_raw if str(s).strip())
        if not scopes:
            raise HTTPException(status_code=422, detail="scopes must include at least one role scope")
        invalid_scopes = sorted(scope for scope in scopes if scope not in _ALLOWED_ROLE_SCOPES)
        if invalid_scopes:
            raise HTTPException(
                status_code=422,
                detail=("invalid role scopes: " + ", ".join(invalid_scopes) + " (allowed: admin, operator, viewer)"),
            )
        expires_in_s = payload.get("expires_in_s")
        if expires_in_s is not None:
            expires_in_s = int(expires_in_s)
            if expires_in_s < 60:
                raise HTTPException(status_code=422, detail="expires_in_s must be >= 60")
        raw_key, record = store.create(
            name,
            scopes=scopes,
            expires_in_s=expires_in_s,
            tenant_id=tenant_id,
        )
        audit_event(
            "api_key.create",
            principal=principal.subject_id,
            source_ip=_source_ip(request),
            detail={"key_id": record.key_id, "name": name},
        )
        return {
            "key": raw_key,
            "key_id": record.key_id,
            "name": record.name,
            "scopes": sorted(record.scopes),
            "created_at": record.created_at,
            "expires_at": record.expires_at,
        }

    @router.get("/keys")
    async def list_api_keys(request: Request) -> list[dict[str, Any]]:
        principal = _principal(request)
        authz = _authz(request)
        if not await authz.is_admin(principal):
            raise HTTPException(status_code=403, detail="admin role required")
        tenant_id = _tenant_scope(principal)
        cfg = request.app.state.uterm_config
        if not cfg.auth.api_keys_enabled:
            raise HTTPException(status_code=403, detail="API key management is disabled")
        store = request.app.state.uterm_api_key_store
        keys = store.list_keys_for_tenant(tenant_id)
        return [
            {
                "key_id": k.key_id,
                "name": k.name,
                "scopes": sorted(k.scopes),
                "created_at": k.created_at,
                "expires_at": k.expires_at,
                "last_used_at": k.last_used_at,
                "revoked": k.revoked,
            }
            for k in keys
        ]

    @router.delete("/keys/{key_id}")
    async def revoke_api_key(request: Request, key_id: str = Path(...)) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        if not await authz.is_admin(principal):
            raise HTTPException(status_code=403, detail="admin role required")
        tenant_id = _tenant_scope(principal)
        cfg = request.app.state.uterm_config
        if not cfg.auth.api_keys_enabled:
            raise HTTPException(status_code=403, detail="API key management is disabled")
        store = request.app.state.uterm_api_key_store
        revoked = store.revoke_for_tenant(key_id, tenant_id)
        if not revoked:
            raise HTTPException(status_code=404, detail="API key not found")
        audit_event(
            "api_key.revoke",
            principal=principal.subject_id,
            source_ip=_source_ip(request),
            detail={"key_id": key_id},
        )
        return {"ok": True, "key_id": key_id}

    return router
