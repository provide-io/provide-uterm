#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Session CRUD, lifecycle, and data routes for the hosted server app.

Exposes:
  GET    /api/sessions                              -- list
  POST   /api/sessions                              -- create
  DELETE /api/sessions                              -- bulk delete
  GET    /api/sessions/{session_id}                  -- get
  PATCH  /api/sessions/{session_id}                  -- patch
  DELETE /api/sessions/{session_id}                  -- delete
  POST   /api/sessions/{session_id}/connect          -- connect
  POST   /api/sessions/{session_id}/disconnect       -- disconnect
  POST   /api/sessions/{session_id}/restart          -- restart
  POST   /api/sessions/{session_id}/mode             -- set input mode
  POST   /api/sessions/{session_id}/clear            -- clear
  POST   /api/sessions/{session_id}/annotate         -- annotate
  POST   /api/sessions/{session_id}/analyze          -- analyze
  GET    /api/sessions/{session_id}/snapshot          -- snapshot
  GET    /api/sessions/{session_id}/events            -- events
  GET    /api/sessions/{session_id}/events/watch      -- watch events
  GET    /api/sessions/{session_id}/recording         -- recording meta
  GET    /api/sessions/{session_id}/recording/entries -- recording entries
  GET    /api/sessions/{session_id}/recording/download -- recording download
"""

from __future__ import annotations

import time
from typing import Annotated, Any, cast

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse

from provide.telemetry import get_tracer
from provide.uterm.server.audit import audit_event
from provide.uterm.server.models import model_dump
from provide.uterm.server.registry import SessionValidationError
from provide.uterm.server.routes._helpers import (
    SessionId,
    authz,
    principal,
    registry,
    session_definition,
    set_span_attrs,
    sid_not_found,
    source_ip,
)


def create_sessions_router() -> APIRouter:
    """Build a router for session CRUD, lifecycle, and data endpoints."""
    router = APIRouter()

    @router.get("/sessions")
    async def list_sessions(
        request: Request,
        tag: Annotated[list[str] | None, Query()] = None,
        connector_type: Annotated[str | None, Query()] = None,
        visibility: Annotated[str | None, Query()] = None,
        state: Annotated[str | None, Query()] = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        sort: Annotated[str, Query()] = "created_at",
        order: Annotated[str, Query()] = "desc",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        p = principal(request)
        az = authz(request)
        pairs = await registry(request).list_sessions_with_definitions()
        results = [model_dump(status) for status, definition in pairs if await az.can_read_session(p, definition)]
        if tag:
            results = [s for s in results if set(tag) & set(s.get("tags", []))]
        if connector_type:
            results = [s for s in results if s.get("connector_type") == connector_type]
        if visibility:
            results = [s for s in results if s.get("visibility") == visibility]
        if state:
            results = [s for s in results if s.get("lifecycle_state") == state]
        if q:
            q_lower = q.lower()
            results = [
                s
                for s in results
                if q_lower in str(s.get("session_id", "")).lower()
                or q_lower in str(s.get("display_name", "")).lower()
                or any(q_lower in t.lower() for t in s.get("tags", []))
            ]
        sort_key = sort if sort in {"created_at", "display_name", "session_id"} else "created_at"
        reverse = order != "asc"
        results.sort(key=lambda s: s.get(sort_key, ""), reverse=reverse)
        return results[offset : offset + limit]

    @router.delete("/sessions")
    async def bulk_delete_sessions(
        request: Request,
        payload: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, int]:
        p = principal(request)
        az = authz(request)
        if not await az.is_admin(p):
            raise HTTPException(status_code=403, detail="admin privileges required for bulk delete")
        filt = payload.get("filter", {})
        filter_state = str(filt.get("state", "")).strip() or None
        older_than_s = filt.get("older_than_s")
        reg = registry(request)
        pairs = await reg.list_sessions_with_definitions()

        now = time.time()
        to_delete: list[str] = []
        for status, definition in pairs:
            if not await az.can_mutate_session(
                p, definition, "session.control.delete"
            ):  # pragma: no cover -- admin always passes
                continue
            dumped = model_dump(status)
            if filter_state and dumped.get("lifecycle_state") != filter_state:
                continue
            if older_than_s is not None:
                stopped_at = dumped.get("stopped_at")
                if stopped_at is None or (now - float(stopped_at)) < float(older_than_s):
                    continue
            to_delete.append(definition.session_id)
        for sid in to_delete:
            await reg.delete_session(sid)
        if to_delete:
            audit_event(
                "session.bulk_delete",
                principal=p.subject_id,
                session_id=",".join(to_delete),
                source_ip=source_ip(request),
                detail={"count": len(to_delete), "filter": filt},
            )
        return {"deleted": len(to_delete)}

    @router.post("/sessions")
    async def create_session(request: Request, payload: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        p = principal(request)
        az = authz(request)
        if not await az.can_create_session(p):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        mutable_payload = dict(payload)
        requested_owner = mutable_payload.get("owner")
        if not await az.is_admin(p):
            if requested_owner not in {None, p.subject_id}:
                raise HTTPException(status_code=403, detail="owner must match authenticated subject")
            mutable_payload["owner"] = p.subject_id
        with get_tracer(__name__).start_as_current_span("uterm.session.create") as span:
            set_span_attrs(
                span,
                session_id=str(mutable_payload.get("session_id", "")),
                operation="session.create",
                principal=p.subject_id,
                http_method="POST",
                http_path="/api/sessions",
            )
            try:
                session = await registry(request).create_session(mutable_payload)
            except SessionValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit_event(
            "session.create",
            principal=p.subject_id,
            session_id=str(mutable_payload.get("session_id", "")),
            source_ip=source_ip(request),
        )
        return model_dump(session)

    @router.get("/sessions/{session_id}")
    async def get_session(request: Request, session_id: SessionId) -> dict[str, Any]:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        if not await az.can_read_session(p, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            session = await registry(request).get_session(session_id)
        except KeyError:
            raise sid_not_found(session_id) from None
        return model_dump(session)

    @router.patch("/sessions/{session_id}")
    async def patch_session(
        request: Request,
        session_id: SessionId,
        payload: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        # connector_config is replaced entirely when present in the payload.
        # Callers must send the full desired config, not just changed keys.
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        if not await az.can_mutate_session(p, definition, "session.control.update"):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            session = await registry(request).update_session(session_id, payload)
        except SessionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError:
            raise sid_not_found(session_id) from None
        return model_dump(session)

    @router.delete("/sessions/{session_id}")
    async def delete_session(request: Request, session_id: SessionId) -> dict[str, bool]:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        if not await az.can_mutate_session(p, definition, "session.control.delete"):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        with get_tracer(__name__).start_as_current_span("uterm.session.delete") as span:
            set_span_attrs(
                span,
                session_id=session_id,
                operation="session.delete",
                principal=p.subject_id,
                http_method="DELETE",
                http_path=f"/api/sessions/{session_id}",
            )
            await registry(request).delete_session(session_id)
        # Revoke any tunnel tokens bound to this session_id -- otherwise an
        # old share_token could authorize a replacement session created later
        # under the same ID.
        tunnel_tokens = cast("dict[str, dict[str, object]]", request.app.state.uterm_tunnel_tokens)
        tunnel_tokens.pop(session_id, None)
        audit_event(
            "session.delete",
            principal=p.subject_id,
            session_id=session_id,
            source_ip=source_ip(request),
        )
        return {"ok": True}

    @router.post("/sessions/{session_id}/connect")
    async def connect_session(request: Request, session_id: SessionId) -> dict[str, Any]:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        if not await az.can_mutate_session(p, definition, "session.control.connect"):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            session = await registry(request).start_session(session_id)
        except KeyError:
            raise sid_not_found(session_id) from None
        return model_dump(session)

    @router.post("/sessions/{session_id}/disconnect")
    async def disconnect_session(request: Request, session_id: SessionId) -> dict[str, Any]:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        # Disconnect is intentionally gated on "connect" so operators who can
        # start sessions can also stop them (symmetric lifecycle control).
        if not await az.can_mutate_session(p, definition, "session.control.connect"):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            session = await registry(request).stop_session(session_id)
        except KeyError:
            raise sid_not_found(session_id) from None
        return model_dump(session)

    @router.post("/sessions/{session_id}/restart")
    async def restart_session(request: Request, session_id: SessionId) -> dict[str, Any]:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        # Same lifecycle symmetry as disconnect above.
        if not await az.can_mutate_session(p, definition, "session.control.connect"):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            session = await registry(request).restart_session(session_id)
        except KeyError:
            raise sid_not_found(session_id) from None
        return model_dump(session)

    @router.post("/sessions/{session_id}/mode")
    async def set_mode(
        request: Request,
        session_id: SessionId,
        payload: Annotated[dict[str, str], Body(...)],
    ) -> dict[str, Any]:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        if not await az.can_mutate_session(p, definition, "session.control.mode"):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        mode = str(payload.get("input_mode", "")).strip()
        if mode not in {"open", "hijack"}:
            raise HTTPException(status_code=422, detail="input_mode must be 'open' or 'hijack'")
        try:
            session = await registry(request).set_mode(session_id, mode)
        except KeyError:
            raise sid_not_found(session_id) from None
        return model_dump(session)

    @router.post("/sessions/{session_id}/clear")
    async def clear_session(request: Request, session_id: SessionId) -> dict[str, Any]:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        if not await az.can_mutate_session(p, definition, "session.control.clear"):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            session = await registry(request).clear_session(session_id)
        except KeyError:
            raise sid_not_found(session_id) from None
        return model_dump(session)

    @router.post("/sessions/{session_id}/annotate")
    async def annotate_session(
        request: Request,
        session_id: SessionId,
        payload: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        if not await az.can_mutate_session(
            p, definition, "session.control.update"
        ):  # pragma: no cover -- admin always passes in dev mode
            raise HTTPException(status_code=403, detail="insufficient privileges")
        label = str(payload.get("label", "")).strip()
        if not label:
            raise HTTPException(status_code=400, detail="label is required")
        severity = str(payload.get("severity", "info"))
        if severity not in ("info", "warning", "high", "critical"):
            raise HTTPException(status_code=400, detail=f"invalid severity: {severity}")
        reg = registry(request)
        runtime = reg.get_runtime(session_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail=f"no active runtime for session: {session_id}")
        annotation_data: dict[str, Any] = {
            "label": label,
            "description": str(payload.get("description", "")),
            "severity": severity,
            "source": "agent",
            "principal": p.subject_id,
        }
        if runtime._logger is not None:
            await runtime._logger.log_event("annotation", annotation_data)
        # Publish to EventBus so live browser observers see annotations in real-time
        hub = request.app.state.uterm_hub
        evt = await hub.append_event(session_id, "annotation", annotation_data)
        audit_event(
            "session.annotate",
            principal=p.subject_id,
            session_id=session_id,
            source_ip=source_ip(request),
            detail=annotation_data,
        )
        return {"ts": time.time(), "seq": evt.get("seq", 0)}

    @router.post("/sessions/{session_id}/analyze")
    async def analyze_session(request: Request, session_id: SessionId) -> dict[str, Any]:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        if not await az.can_read_session(p, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            analysis = await registry(request).analyze_session(session_id)
        except KeyError:
            raise sid_not_found(session_id) from None
        return {"session_id": session_id, "analysis": analysis}

    @router.get("/sessions/{session_id}/snapshot")
    async def snapshot(request: Request, session_id: SessionId) -> dict[str, Any] | None:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        if not await az.can_read_session(p, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        # Pass the Request as the redaction recipient so a configured output
        # policy redacts the snapshot to the requester's role (M5) — the same
        # treatment the live broadcast and WS initial-snapshot paths apply.
        return await registry(request).last_snapshot(session_id, recipient=request)

    @router.get("/sessions/{session_id}/events")
    async def events(
        request: Request,
        session_id: SessionId,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        if not await az.can_read_session(p, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        return await registry(request).events(session_id, limit=limit)

    @router.get("/sessions/{session_id}/events/watch")
    async def watch_events(
        request: Request,
        session_id: SessionId,
        timeout_ms: Annotated[int, Query(ge=100, le=30000)] = 5000,
        event_types: Annotated[str | None, Query(max_length=500)] = None,
        pattern: Annotated[str | None, Query(max_length=200)] = None,
        max_events: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        if not await az.can_read_session(p, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        return await registry(request).watch_session_events(
            session_id,
            timeout_ms=timeout_ms,
            event_types=event_types.split(",") if event_types else None,
            pattern=pattern,
            max_events=max_events,
        )

    @router.get("/sessions/{session_id}/recording")
    async def recording(request: Request, session_id: SessionId) -> dict[str, Any]:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        if not await az.can_read_recording(p, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            return await registry(request).recording_meta(session_id)
        except KeyError:
            raise sid_not_found(session_id) from None

    @router.get("/sessions/{session_id}/recording/entries")
    async def recording_entries(
        request: Request,
        session_id: SessionId,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        offset: Annotated[int | None, Query(ge=0)] = None,
        event: Annotated[str | None, Query(max_length=100)] = None,
    ) -> list[dict[str, Any]]:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        if not await az.can_read_recording(p, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            return await registry(request).recording_entries(session_id, limit=limit, offset=offset, event=event)
        except KeyError:
            raise sid_not_found(session_id) from None

    @router.get("/sessions/{session_id}/recording/download")
    async def recording_download(request: Request, session_id: SessionId) -> FileResponse:
        p = principal(request)
        az = authz(request)
        definition = await session_definition(request, session_id)
        if not await az.can_read_recording(p, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        try:
            path = await registry(request).recording_path(session_id)
        except KeyError:
            raise sid_not_found(session_id) from None
        if path is None or not path.exists():
            raise HTTPException(status_code=404, detail="recording not available")
        recording_cfg = getattr(getattr(request.app.state, "uterm_config", None), "recording", None)
        if recording_cfg is None or not path.resolve().is_relative_to(recording_cfg.directory.resolve()):
            raise HTTPException(status_code=404, detail="recording not available")
        return FileResponse(path, filename=path.name, media_type="application/json")

    return router
