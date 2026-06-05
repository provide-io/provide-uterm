#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Mutation-enforced at killed==100 ([tool.mutmut]); bound suite: tests/server/test_routes_mutation_killing.py (router-endpoint extraction, mocked Request).
"""REST API routes for connection profiles."""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from fastapi import APIRouter, Body, HTTPException, Path, Request
from pydantic import ValidationError

from provide.uterm.server.egress import EgressBlockedError
from provide.uterm.server.models import model_dump
from provide.uterm.server.profiles import ConnectionProfile
from provide.uterm.server.registry import SessionValidationError

if TYPE_CHECKING:
    from provide.uterm.server.auth import Principal
    from provide.uterm.server.authorization import AuthorizationService
    from provide.uterm.server.profiles import FileProfileStore
    from provide.uterm.server.registry import SessionRegistry

_ProfileId = Annotated[str, Path(pattern=r"^[\w\-]+$")]


def _store(request: Request) -> FileProfileStore:
    return cast("FileProfileStore", request.app.state.uterm_profile_store)


def _authz(request: Request) -> AuthorizationService:
    return cast("AuthorizationService", request.app.state.uterm_authz)


def _registry(request: Request) -> SessionRegistry:
    return cast("SessionRegistry", request.app.state.uterm_registry)


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "uterm_principal", None)
    if principal is None:
        raise HTTPException(status_code=500, detail="principal was not resolved")
    return cast("Principal", principal)


def _not_found(profile_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"unknown profile: {profile_id}")


def create_profiles_router() -> APIRouter:
    router = APIRouter(prefix="/api/profiles")

    @router.get("")
    async def list_profiles(request: Request) -> list[dict[str, Any]]:
        principal = _principal(request)
        authz = _authz(request)
        store = _store(request)
        if await authz.is_admin(principal):
            profiles = await store.list_profiles()
        else:
            profiles = await store.list_profiles(owner=principal.subject_id)
        return [p.model_dump(mode="python") for p in profiles]

    @router.get("/{profile_id}")
    async def get_profile(request: Request, profile_id: _ProfileId) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        store = _store(request)
        profile = await store.get_profile(profile_id)
        if profile is None:
            raise _not_found(profile_id)
        if not await authz.can_read_profile(principal, profile):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        return profile.model_dump(mode="python")

    @router.post("")
    async def create_profile(request: Request, payload: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        if not await authz.can_create_session(principal):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        store = _store(request)
        now = time.time()
        tags_raw = payload.get("tags", [])
        tags = [str(t).strip() for t in tags_raw if str(t).strip()] if isinstance(tags_raw, list) else []
        connector_type = cast(
            "Literal['ssh', 'telnet', 'websocket', 'ushell', 'shell']",
            str(payload.get("connector_type", "ssh")),
        )
        input_mode = cast("Literal['open', 'hijack']", str(payload.get("input_mode", "open")))
        visibility = cast("Literal['private', 'shared']", str(payload.get("visibility", "private")))
        profile = ConnectionProfile(
            profile_id=f"profile-{uuid.uuid4().hex[:12]}",
            owner=principal.subject_id,
            name=str(payload.get("name") or "Unnamed").strip(),
            connector_type=connector_type,
            host=str(payload["host"]).strip() or None if payload.get("host") else None,
            port=int(payload["port"]) if payload.get("port") else None,
            username=str(payload["username"]).strip() or None if payload.get("username") else None,
            tags=tags,
            input_mode=input_mode,
            recording_enabled=bool(payload.get("recording_enabled", False)),
            visibility=visibility,
            created_at=now,
            updated_at=now,
        )
        created = await store.create_profile(profile)
        return created.model_dump(mode="python")

    @router.put("/{profile_id}")
    async def update_profile(
        request: Request,
        profile_id: _ProfileId,
        payload: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        store = _store(request)
        profile = await store.get_profile(profile_id)
        if profile is None:
            raise _not_found(profile_id)
        if not await authz.can_mutate_profile(principal, profile):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        allowed = {"name", "host", "port", "username", "tags", "input_mode", "recording_enabled", "visibility"}
        updates = {k: v for k, v in payload.items() if k in allowed}
        try:
            updated = await store.update_profile(profile_id, updates)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if updated is None:
            raise _not_found(profile_id)
        return updated.model_dump(mode="python")

    @router.delete("/{profile_id}")
    async def delete_profile(request: Request, profile_id: _ProfileId) -> dict[str, bool]:
        principal = _principal(request)
        authz = _authz(request)
        store = _store(request)
        profile = await store.get_profile(profile_id)
        if profile is None:
            raise _not_found(profile_id)
        if not await authz.can_mutate_profile(principal, profile):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        await store.delete_profile(profile_id)
        return {"ok": True}

    @router.post("/{profile_id}/connect")
    async def connect_from_profile(
        request: Request,
        profile_id: _ProfileId,
        payload: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        store = _store(request)
        registry = _registry(request)
        profile = await store.get_profile(profile_id)
        if profile is None:
            raise _not_found(profile_id)
        if not await authz.can_read_profile(principal, profile):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        if not await authz.can_create_session(principal):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        connector_config: dict[str, Any] = {}
        if profile.host:
            connector_config["host"] = profile.host
        if profile.port:
            connector_config["port"] = profile.port
        if profile.username:
            connector_config["username"] = profile.username
        if payload.get("password"):
            connector_config["password"] = payload["password"]
        session_id = f"connect-{uuid.uuid4().hex[:12]}"
        session_payload: dict[str, Any] = {
            "session_id": session_id,
            "display_name": profile.name,
            "connector_type": profile.connector_type,
            "connector_config": connector_config,
            "input_mode": profile.input_mode,
            "tags": list(profile.tags),
            "auto_start": True,
            "ephemeral": True,
            "visibility": "private",
            "owner": principal.subject_id,
        }
        if profile.recording_enabled:
            session_payload["recording_enabled"] = True
        try:
            session = await registry.create_session(session_payload)
        except EgressBlockedError as exc:
            # The registry chokepoint blocks a metadata/internal connector
            # target with EgressBlockedError (a ValueError subclass). Map it to
            # 422 like the other connect routes instead of letting it fall into
            # the generic ValueError→409 branch below.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SessionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        cfg = request.app.state.uterm_config
        url = f"{cfg.ui.app_path}/session/{session_id}"
        return {"session_id": session_id, "url": url, **model_dump(session)}

    return router
