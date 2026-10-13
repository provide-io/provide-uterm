#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Mutation-enforced with the rest of routes/ ([tool.mutmut]); bound suites:
#   tests/server/test_viewer_revocation.py              end to end + the cutoff
#   tests/server/test_routes_viewers_mutation_killing.py the handler, mocked
"""Revoke a session's share viewers.

Exposes:
  POST /api/sessions/{session_id}/viewers/revoke -- record a cutoff and close live share viewers

A runtime-native route rather than a shared ``RouteDef``: share-viewer tokens
(``share:{session_id}:...`` subjects) are a property of this server's
authorization, and the Cloudflare Worker has no such grant to revoke.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from provide.uterm.api_routes import HttpMethod
from provide.uterm.server.audit import audit_event
from provide.uterm.server.authorization import ShareViewerRevocationsFullError
from provide.uterm.server.routes._helpers import (
    SessionId,
    authz,
    principal,
    registry,
    session_definition,
    source_ip,
)
from provide.uterm.server.viewer_revocation import (
    VIEWER_REVOKED_CLOSE_CODE,
    VIEWER_REVOKED_REASON,
    close_revoked_viewers,
    share_viewer_streams,
)

VIEWERS_REVOKE_PATH = "/sessions/{session_id}/viewers/revoke"


async def revoke_viewers(request: Request, session_id: SessionId) -> dict[str, Any]:
    """Refuse the session's existing share-viewer tokens and close their sockets.

    ``closed`` counts both: browser sockets closed with 4410 and event streams
    ended with a ``viewer_access_revoked`` event.

    Allowed to whoever may change the session (``session.control.update``): its
    owner, a session-scoped admin, a global admin. Tokens issued after the
    returned ``revoked_at`` are unaffected, so an embedder that later re-opens
    the session simply mints new ones.
    """
    p = principal(request)
    az = authz(request)
    definition = await session_definition(request, session_id)
    if not await az.can_mutate_session(p, definition, "session.control.update"):
        raise HTTPException(status_code=403, detail="insufficient privileges")
    # Cutoffs of sessions that have since been deleted go first: nothing is left
    # for them to refuse, and keeping them is what would fill the table.
    reg = registry(request)
    for stale in az.share_viewer_revoked_sessions():
        if await reg.get_definition(stale) is None:
            az.forget_share_viewers(stale)
    try:
        revoked_at = az.revoke_share_viewers(session_id)
    except ShareViewerRevocationsFullError:
        raise HTTPException(status_code=429, detail="too many sessions with revoked viewers; try again later") from None
    closed = await close_revoked_viewers(request.app.state.uterm_hub, az, session_id)
    closed += share_viewer_streams(request.app).revoke(az, session_id)
    audit_event(
        "session.viewers.revoke",
        principal=p.subject_id,
        session_id=session_id,
        detail={"closed": closed, "revoked_at": revoked_at},
        source_ip=source_ip(request),
    )
    return {
        "session_id": session_id,
        "revoked_at": revoked_at,
        "closed": closed,
        "close_code": VIEWER_REVOKED_CLOSE_CODE,
        "close_reason": VIEWER_REVOKED_REASON,
    }


def register_viewer_routes(router: APIRouter) -> None:
    """Bind the viewer-revocation route."""
    # The method comes from HttpMethod rather than a literal: FastAPI upper-cases
    # whatever it is given, so a "post" mutant of a literal would be equivalent.
    router.add_api_route(
        VIEWERS_REVOKE_PATH, revoke_viewers, methods=[HttpMethod.POST.value], name="sessions.viewers.revoke"
    )
