#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Mutation-enforced at killed==100 ([tool.mutmut]); bound suite: tests/server/test_routes_mutation_killing.py (router-endpoint extraction, mocked Request).
"""API key management routes (admin-only)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Path, Request

from provide.uterm.server.audit import audit_event
from provide.uterm.server.routes._helpers import authz as _authz
from provide.uterm.server.routes._helpers import principal as _principal
from provide.uterm.server.routes._helpers import source_ip as _source_ip

_ALLOWED_ROLE_SCOPES = frozenset({"viewer", "operator", "admin"})


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
        if "tenant_id" in payload:
            raise HTTPException(status_code=422, detail="tenant_id is server-assigned and cannot be supplied")
        expires_in_s = payload.get("expires_in_s")
        if expires_in_s is not None:
            expires_in_s = int(expires_in_s)
            if expires_in_s < 60:
                raise HTTPException(status_code=422, detail="expires_in_s must be >= 60")
        # Tenant is derived from the authenticated principal, never client input:
        # a tenant-scoped admin mints keys bound to their own tenant (isolated);
        # a system admin (no tenant) mints tenant-less system keys.
        tenant = principal.tenant_id
        if tenant:
            raw_key, record = store.create_for_tenant(tenant, name, scopes=scopes, expires_in_s=expires_in_s)
        else:
            raw_key, record = store.create(name, scopes=scopes, expires_in_s=expires_in_s)
        audit_event(
            "api_key.create",
            principal=principal.subject_id,
            source_ip=_source_ip(request),
            detail={"key_id": record.key_id, "name": name, "tenant_id": record.tenant_id},
        )
        return {
            "key": raw_key,
            "key_id": record.key_id,
            "name": record.name,
            "tenant_id": record.tenant_id,
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
        cfg = request.app.state.uterm_config
        if not cfg.auth.api_keys_enabled:
            raise HTTPException(status_code=403, detail="API key management is disabled")
        store = request.app.state.uterm_api_key_store
        # A tenant admin sees only their tenant's (non-revoked) keys; a system
        # admin sees every key.
        tenant = principal.tenant_id
        keys = store.list_keys_for_tenant(tenant) if tenant else store.list_keys()
        return [
            {
                "key_id": k.key_id,
                "name": k.name,
                "tenant_id": k.tenant_id,
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
        cfg = request.app.state.uterm_config
        if not cfg.auth.api_keys_enabled:
            raise HTTPException(status_code=403, detail="API key management is disabled")
        store = request.app.state.uterm_api_key_store
        # A tenant admin can revoke only keys owned by their tenant; a system
        # admin can revoke any key.
        tenant = principal.tenant_id
        revoked = store.revoke_for_tenant(key_id, tenant) if tenant else store.revoke(key_id)
        if not revoked:
            raise HTTPException(status_code=404, detail=f"unknown key: {key_id}")
        audit_event(
            "api_key.revoke",
            principal=principal.subject_id,
            source_ip=_source_ip(request),
            detail={"key_id": key_id},
        )
        return {"ok": True, "key_id": key_id}

    return router
