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
from provide.terminal.server.routes.sse import create_sse_router
from provide.terminal.server.routes.webhooks import create_webhook_router

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
    router.include_router(create_sse_router())
    router.include_router(create_webhook_router())

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

    @router.get("/sessions/{session_id}/events/watch")
    async def watch_events(
        request: Request,
        session_id: _SessionId,
        timeout_ms: Annotated[int, Query(ge=100, le=30000)] = 5000,
        event_types: Annotated[str | None, Query(max_length=500)] = None,
        pattern: Annotated[str | None, Query(max_length=200)] = None,
        max_events: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        definition = await _session_definition(request, session_id)
        if not authz.can_read_session(principal, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        return await _registry(request).watch_session_events(  # type: ignore[attr-defined]
            session_id,
            timeout_ms=timeout_ms,
            event_types=event_types.split(",") if event_types else None,
            pattern=pattern,
            max_events=max_events,
        )

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

    @router.post("/tunnels")
    async def create_tunnel(request: Request, payload: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        """Create a tunnel session for ``uterm share``.

        Returns tunnel_id, ws_endpoint, share_url, control_url, and expires_at.
        Accepts optional ``ttl_s`` in payload to override server default.
        """
        import secrets
        import time

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

        source_ip = str(getattr(request.client, "host", "unknown")) if request.client else "unknown"

        registry = _registry(request)
        base = cfg.server.public_base_url or str(request.base_url).rstrip("/")
        ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
        tunnel_tokens = cast("dict[str, dict[str, object]]", request.app.state.uterm_tunnel_tokens)

        try:
            await registry.create_session(
                {
                    "session_id": tunnel_id,
                    "display_name": display_name,
                    "connector_type": "websocket",
                    "connector_config": {"tunnel_type": tunnel_type},
                    "input_mode": "open",
                    "auto_start": False,
                    "ephemeral": True,
                    "visibility": "public",
                    "recording_enabled": True,
                }
            )
        except SessionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        tunnel_tokens[tunnel_id] = {
            "worker_token": worker_token,
            "share_token": share_token,
            "control_token": control_token,
            "created_at": now,
            "expires_at": expires_at,
            "issued_ip": source_ip if tunnel_cfg.ip_binding else None,
            "tunnel_type": tunnel_type,
        }

        from provide.telemetry import get_logger

        get_logger(__name__).info(
            "tunnel_token_created session_id=%s ttl_s=%d source_ip=%s",
            tunnel_id,
            ttl_s,
            source_ip,
        )

        return {
            "tunnel_id": tunnel_id,
            "display_name": display_name,
            "tunnel_type": tunnel_type,
            "ws_endpoint": f"{ws_base}/tunnel/{tunnel_id}",
            "worker_token": worker_token,
            "share_url": f"{base}{cfg.ui.app_path}/session/{tunnel_id}?token={share_token}",
            "control_url": f"{base}{cfg.ui.app_path}/operator/{tunnel_id}?token={control_token}",
            "expires_at": expires_at,
        }

    @router.delete("/tunnels/{tunnel_id}/tokens")
    async def revoke_tunnel_tokens(request: Request, tunnel_id: _SessionId) -> dict[str, Any]:
        """Revoke all tokens for a tunnel session."""
        tunnel_tokens = cast("dict[str, dict[str, object]]", request.app.state.uterm_tunnel_tokens)
        removed = tunnel_tokens.pop(tunnel_id, None)
        from provide.telemetry import get_logger

        get_logger(__name__).info("tunnel_token_revoked session_id=%s found=%s", tunnel_id, removed is not None)
        return {"ok": True, "session_id": tunnel_id}

    @router.post("/tunnels/{tunnel_id}/tokens/rotate")
    async def rotate_tunnel_tokens(request: Request, tunnel_id: _SessionId) -> dict[str, Any]:
        """Rotate all tokens for a tunnel session. Returns new tokens and URLs."""
        import secrets
        import time

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
        source_ip = str(getattr(request.client, "host", "unknown")) if request.client else "unknown"

        tunnel_tokens[tunnel_id] = {
            "worker_token": worker_token,
            "share_token": share_token,
            "control_token": control_token,
            "created_at": now,
            "expires_at": now + ttl_s,
            "issued_ip": source_ip if cfg.tunnel.ip_binding else None,
            "tunnel_type": str(old.get("tunnel_type", "terminal")),
        }

        base = cfg.server.public_base_url or str(request.base_url).rstrip("/")
        ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
        from provide.telemetry import get_logger

        get_logger(__name__).info("tunnel_token_rotated session_id=%s source_ip=%s", tunnel_id, source_ip)

        return {
            "tunnel_id": tunnel_id,
            "ws_endpoint": f"{ws_base}/tunnel/{tunnel_id}",
            "worker_token": worker_token,
            "share_url": f"{base}{cfg.ui.app_path}/session/{tunnel_id}?token={share_token}",
            "control_url": f"{base}{cfg.ui.app_path}/operator/{tunnel_id}?token={control_token}",
            "expires_at": now + ttl_s,
        }

    return router
