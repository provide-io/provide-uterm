#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""HTTP API routes for the hosted server app."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Body, HTTPException, Path, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse

from provide.terminal.server.models import model_dump
from provide.terminal.server.registry import SessionValidationError

# Validated session_id path parameter — rejects path-unsafe characters.
_SessionId = Annotated[str, Path(pattern=r"^[\w\-]+$")]

if TYPE_CHECKING:
    from provide.terminal.server.auth import Principal
    from provide.terminal.server.authorization import AuthorizationService
    from provide.terminal.server.models import SessionDefinition
    from provide.terminal.server.registry import SessionRegistry


def _registry(request: Request) -> SessionRegistry:
    return cast("SessionRegistry", request.app.state.uterm_registry)


def _authz(request: Request) -> AuthorizationService:
    return cast("AuthorizationService", request.app.state.uterm_authz)


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "uterm_principal", None)
    if principal is None:
        raise HTTPException(status_code=500, detail="principal was not resolved")
    return cast("Principal", principal)


async def _session_definition(request: Request, session_id: str) -> SessionDefinition:
    session = await _registry(request).get_definition(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
    return session


def create_api_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    async def health(request: Request, response: Response) -> dict[str, object]:
        ready = getattr(request.app.state, "uterm_registry", None) is not None
        if not ready:
            response.status_code = 503
            return {"ok": False, "ready": False, "service": "uterm-server"}
        return {"ok": True, "ready": True, "service": "uterm-server"}

    @router.get("/metrics")
    async def metrics(request: Request) -> dict[str, object]:
        payload = getattr(request.app.state, "uterm_metrics", {})
        if not isinstance(payload, dict):
            payload = {}
        return {"metrics": payload}

    @router.get("/metrics/prometheus")
    async def metrics_prometheus(request: Request) -> PlainTextResponse:
        payload = getattr(request.app.state, "uterm_metrics", {})
        if not isinstance(payload, dict):
            payload = {}
        lines: list[str] = []
        for name in sorted(payload):
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {payload[name]}")
        body = "\n".join(lines) + ("\n" if lines else "")
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")

    @router.get("/sessions")
    async def list_sessions(request: Request) -> list[dict[str, Any]]:
        principal = _principal(request)
        authz = _authz(request)
        pairs = await _registry(request).list_sessions_with_definitions()
        return [model_dump(status) for status, definition in pairs if authz.can_read_session(principal, definition)]

    @router.post("/sessions")
    async def create_session(request: Request, payload: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        if not authz.can_create_session(principal):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        mutable_payload = dict(payload)
        requested_owner = mutable_payload.get("owner")
        if not authz.is_admin(principal):
            if requested_owner not in {None, principal.subject_id}:
                raise HTTPException(status_code=403, detail="owner must match authenticated subject")
            mutable_payload["owner"] = principal.subject_id
        try:
            session = await _registry(request).create_session(mutable_payload)
        except SessionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return model_dump(session)

    def _sid_not_found(session_id: str) -> HTTPException:
        return HTTPException(status_code=404, detail=f"unknown session: {session_id}")

    @router.get("/sessions/{session_id}")
    async def get_session(request: Request, session_id: _SessionId) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        if not authz.can_read_session(principal, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            session = await _registry(request).get_session(session_id)
        except KeyError:
            raise _sid_not_found(session_id) from None
        return model_dump(session)

    @router.patch("/sessions/{session_id}")
    async def patch_session(
        request: Request,
        session_id: _SessionId,
        payload: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        # connector_config is replaced entirely when present in the payload.
        # Callers must send the full desired config, not just changed keys.
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        if not authz.can_mutate_session(principal, definition, "session.control.update"):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            session = await _registry(request).update_session(session_id, payload)
        except SessionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError:
            raise _sid_not_found(session_id) from None
        return model_dump(session)

    @router.delete("/sessions/{session_id}")
    async def delete_session(request: Request, session_id: _SessionId) -> dict[str, bool]:
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        if not authz.can_mutate_session(principal, definition, "session.control.delete"):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        await _registry(request).delete_session(session_id)
        return {"ok": True}

    @router.post("/sessions/{session_id}/connect")
    async def connect_session(request: Request, session_id: _SessionId) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        if not authz.can_mutate_session(principal, definition, "session.control.connect"):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            session = await _registry(request).start_session(session_id)
        except KeyError:
            raise _sid_not_found(session_id) from None
        return model_dump(session)

    @router.post("/sessions/{session_id}/disconnect")
    async def disconnect_session(request: Request, session_id: _SessionId) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        # Disconnect is intentionally gated on "connect" so operators who can
        # start sessions can also stop them (symmetric lifecycle control).
        if not authz.can_mutate_session(principal, definition, "session.control.connect"):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            session = await _registry(request).stop_session(session_id)
        except KeyError:
            raise _sid_not_found(session_id) from None
        return model_dump(session)

    @router.post("/sessions/{session_id}/restart")
    async def restart_session(request: Request, session_id: _SessionId) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        # Same lifecycle symmetry as disconnect above.
        if not authz.can_mutate_session(principal, definition, "session.control.connect"):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            session = await _registry(request).restart_session(session_id)
        except KeyError:
            raise _sid_not_found(session_id) from None
        return model_dump(session)

    @router.post("/sessions/{session_id}/mode")
    async def set_mode(
        request: Request,
        session_id: _SessionId,
        payload: Annotated[dict[str, str], Body(...)],
    ) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        if not authz.can_mutate_session(principal, definition, "session.control.mode"):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        mode = str(payload.get("input_mode", "")).strip()
        if mode not in {"open", "hijack"}:
            raise HTTPException(status_code=422, detail="input_mode must be 'open' or 'hijack'")
        try:
            session = await _registry(request).set_mode(session_id, mode)
        except KeyError:
            raise _sid_not_found(session_id) from None
        return model_dump(session)

    @router.post("/sessions/{session_id}/clear")
    async def clear_session(request: Request, session_id: _SessionId) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        if not authz.can_mutate_session(principal, definition, "session.control.clear"):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            session = await _registry(request).clear_session(session_id)
        except KeyError:
            raise _sid_not_found(session_id) from None
        return model_dump(session)

    @router.post("/sessions/{session_id}/analyze")
    async def analyze_session(request: Request, session_id: _SessionId) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        if not authz.can_read_session(principal, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            analysis = await _registry(request).analyze_session(session_id)
        except KeyError:
            raise _sid_not_found(session_id) from None
        return {"session_id": session_id, "analysis": analysis}

    @router.get("/sessions/{session_id}/snapshot")
    async def snapshot(request: Request, session_id: _SessionId) -> dict[str, Any] | None:
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        if not authz.can_read_session(principal, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        return await _registry(request).last_snapshot(session_id)

    @router.get("/sessions/{session_id}/events")
    async def events(
        request: Request,
        session_id: _SessionId,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        if not authz.can_read_session(principal, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        return await _registry(request).events(session_id, limit=limit)

    @router.get("/sessions/{session_id}/recording")
    async def recording(request: Request, session_id: _SessionId) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        if not authz.can_read_recording(principal, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            return await _registry(request).recording_meta(session_id)
        except KeyError:
            raise _sid_not_found(session_id) from None

    @router.get("/sessions/{session_id}/recording/entries")
    async def recording_entries(
        request: Request,
        session_id: _SessionId,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        offset: Annotated[int | None, Query(ge=0)] = None,
        event: Annotated[str | None, Query(max_length=100)] = None,
    ) -> list[dict[str, Any]]:
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        if not authz.can_read_recording(principal, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            return await _registry(request).recording_entries(session_id, limit=limit, offset=offset, event=event)
        except KeyError:
            raise _sid_not_found(session_id) from None

    @router.get("/sessions/{session_id}/recording/download")
    async def recording_download(request: Request, session_id: _SessionId) -> FileResponse:
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        if not authz.can_read_recording(principal, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            path = await _registry(request).recording_path(session_id)
        except KeyError:
            raise _sid_not_found(session_id) from None
        if path is None or not path.exists():
            raise HTTPException(status_code=404, detail="recording not available")
        recording_cfg = getattr(getattr(request.app.state, "uterm_config", None), "recording", None)
        if recording_cfg is None or not path.resolve().is_relative_to(recording_cfg.directory.resolve()):
            raise HTTPException(status_code=404, detail="recording not available")
        return FileResponse(path, filename=path.name, media_type="application/json")

    @router.post("/connect")
    async def quick_connect(request: Request, payload: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        if not authz.can_create_session(principal):
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
            "owner": principal.subject_id,
        }
        if payload.get("recording_enabled"):
            session_payload["recording_enabled"] = True
        try:
            session = await _registry(request).create_session(session_payload)
        except SessionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        cfg = request.app.state.uterm_config
        url = f"{cfg.ui.app_path}/session/{session_id}"
        return {"session_id": session_id, "url": url, **model_dump(session)}

    return router
