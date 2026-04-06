#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""SSE streaming route for the hosted server app.

Exposes ``GET /api/sessions/{session_id}/events/stream`` which delivers events
from the EventBus as a persistent ``text/event-stream`` response.  Each event
is formatted as ``data: {json}\\n\\n``.  A heartbeat line is emitted every
``_HEARTBEAT_S`` seconds of idle time so proxies don't close the connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, cast

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse

if TYPE_CHECKING:
    from provide.terminal.server.auth import Principal
    from provide.terminal.server.authorization import AuthorizationService
    from provide.terminal.server.registry import SessionRegistry

_HEARTBEAT_S = 15.0

# Validated session_id path parameter — rejects path-unsafe characters.
_SessionId = Annotated[str, Path(pattern=r"^[\w\-]+$")]


def _registry(request: Request) -> SessionRegistry:
    return cast("SessionRegistry", request.app.state.uterm_registry)


def _authz(request: Request) -> AuthorizationService:
    return cast("AuthorizationService", request.app.state.uterm_authz)


def create_sse_router() -> APIRouter:
    router = APIRouter()

    @router.get("/sessions/{session_id}/events/stream")
    async def stream_events(
        request: Request,
        session_id: _SessionId,
        event_types: Annotated[str | None, Query(max_length=500)] = None,
        pattern: Annotated[str | None, Query(max_length=200)] = None,
    ) -> StreamingResponse:
        """Stream session events as Server-Sent Events.

        Keeps the connection open and pushes ``data: {json}\\n\\n`` for each
        event.  A heartbeat is sent every 15 s of idle time.  The stream ends
        when the worker disconnects (a final ``worker_disconnected`` event is
        sent) or the client closes the connection.

        When the server has no EventBus configured, the stream closes
        immediately after sending any buffered events.
        """
        principal = getattr(request.state, "uterm_principal", None)
        if principal is None:  # pragma: no cover — middleware always sets this
            raise HTTPException(status_code=500, detail="principal was not resolved")
        principal = cast("Principal", principal)

        authz = _authz(request)
        registry = _registry(request)

        definition = await registry.get_definition(session_id)
        if definition is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        if not authz.can_read_session(principal, definition):
            raise HTTPException(status_code=403, detail="insufficient privileges")

        event_types_list = [e.strip() for e in event_types.split(",") if e.strip()] if event_types else None
        generator = registry.stream_session_events(
            session_id,
            event_types=event_types_list,
            pattern=pattern,
            heartbeat_s=_HEARTBEAT_S,
        )
        return StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
