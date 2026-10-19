#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Mutation-enforced with the rest of routes/ ([tool.mutmut]); bound suites:
#   tests/server/test_viewer_revocation.py              end to end + the cutoff
#   tests/server/test_routes_viewers_mutation_killing.py the handler, mocked
#   tests/server/test_viewer_subject_revocation.py      the per-subject revoke end to end
"""Revoke a session's share viewers.

Exposes:
  POST /api/sessions/{session_id}/viewers/revoke -- record a cutoff and close live share viewers,
    every one of the session's or, with ``{"subject": "share:{session_id}:..."}``, that subject's only

A runtime-native route rather than a shared ``RouteDef``: share-viewer tokens
(``share:{session_id}:...`` subjects) are a property of this server's
authorization, and the Cloudflare Worker has no such grant to revoke.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from provide.uterm.api_routes import HttpMethod
from provide.uterm.server.audit import audit_event
from provide.uterm.server.authorization import (
    SHARE_VIEWER_REVOKE_BODY_MAX_BYTES,
    SHARE_VIEWER_SUBJECT_MAX_BYTES,
    ShareViewerRevocationsFullError,
    ShareViewerSubjectRevocationsFullError,
)
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


def _invalid_body() -> HTTPException:
    return HTTPException(status_code=422, detail="invalid request body")


async def _requested_subject(request: Request, session_id: str) -> str | None:
    """The share subject the body names, or ``None`` to revoke every share viewer.

    No body, a blank one, ``null`` and ``{}`` all mean the whole session, as
    before the body existed (``null`` is "no body" across this server, as in an
    optional FastAPI body). Anything else must be exactly
    ``{"subject": "<string>"}``: an unknown key (a misspelt ``subject``) or a
    ``null`` subject is refused rather than read as "every viewer", which would
    close sockets the caller meant to leave open. Read by hand rather than as a
    Pydantic body so the refusals are the same ``detail`` strings the other
    ports answer with.
    """
    # Read only until the cap is passed: the largest body worth reading is a
    # 512-byte subject in its envelope, so a longer one is refused as soon as
    # the chunk that crosses the cap arrives, and the rest is never read.
    received = bytearray()
    async for chunk in request.stream():
        received += chunk
        if len(received) > SHARE_VIEWER_REVOKE_BODY_MAX_BYTES:
            raise _invalid_body()
    # JSON's own whitespace only, as every port trims, so a body of other
    # whitespace is malformed everywhere rather than empty in some ports.
    raw = bytes(received).strip(b" \t\r\n")
    if not raw:
        return None
    # Decoded as strict UTF-8 first: ``json.loads`` given bytes would also take
    # UTF-16 and a byte-order mark, which the other ports refuse. UTF-8 is the
    # default codec; naming it would only give mutmut a case mutant that
    # decodes the same. A deeply nested body can exhaust json's recursion (on
    # Python 3.11 past 1000 levels, well inside the cap), which is as malformed
    # as any other.
    try:
        body = json.loads(raw.decode())
    except (ValueError, RecursionError):
        raise _invalid_body() from None
    if body is None:
        return None
    if not isinstance(body, dict) or not body.keys() <= {"subject"}:
        raise _invalid_body()
    if "subject" not in body:
        return None
    subject = body["subject"]
    if not isinstance(subject, str):
        raise _invalid_body()
    # JSON can spell a lone surrogate (``"\ud800"``), which is not text and
    # cannot be encoded (as UTF-8, the default codec, left unnamed for the
    # reason above): refused like any other malformed body.
    try:
        size = len(subject.encode())
    except UnicodeEncodeError:
        raise _invalid_body() from None
    # The prefix is what ties a share token to its session (``is_share_viewer``),
    # so a subject without it could never match one of this session's viewers.
    prefix = f"share:{session_id}:"
    if not subject.startswith(prefix) or subject == prefix or size > SHARE_VIEWER_SUBJECT_MAX_BYTES:
        raise HTTPException(status_code=422, detail="subject must be a share subject of this session")
    return subject


async def revoke_viewers(request: Request, session_id: SessionId) -> dict[str, Any]:
    """Refuse the session's existing share-viewer tokens and close their sockets.

    With a ``subject`` in the body only that share subject's tokens and sockets
    are; the session's other viewers keep watching, and the answer echoes the
    ``subject`` it applied.

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
    # Judged before anything is pruned or recorded, so a refused body changes nothing.
    subject = await _requested_subject(request, session_id)
    # Cutoffs of sessions that have since been deleted go first: nothing is left
    # for them to refuse, and keeping them is what would fill the table.
    # One pass under the registry lock rather than a lookup per held cutoff.
    for stale in await registry(request).unknown_sessions(az.share_viewer_revoked_sessions()):
        az.forget_share_viewers(stale)
    if subject is None:
        revoked_at = await _revoke_session(request, session_id)
    else:
        try:
            revoked_at = az.revoke_share_viewer_subject(session_id, subject)
        except ShareViewerSubjectRevocationsFullError:
            raise HTTPException(
                status_code=429, detail="too many viewers of this session revoked one by one; revoke them all instead"
            ) from None
        except ShareViewerRevocationsFullError:
            raise HTTPException(
                status_code=429, detail="too many viewers revoked one by one; try again later"
            ) from None
    # The close predicate is the same either way: the subject cutoff just
    # recorded refuses that subject's sockets and nobody else's.
    closed = await close_revoked_viewers(request.app.state.uterm_hub, az, session_id)
    closed += share_viewer_streams(request.app).revoke(az, session_id)
    detail: dict[str, Any] = {"closed": closed, "revoked_at": revoked_at}
    if subject is not None:
        detail["subject"] = subject
    audit_event(
        "session.viewers.revoke",
        principal=p.subject_id,
        session_id=session_id,
        detail=detail,
        source_ip=source_ip(request),
    )
    answer: dict[str, Any] = {
        "session_id": session_id,
        "revoked_at": revoked_at,
        "closed": closed,
        "close_code": VIEWER_REVOKED_CLOSE_CODE,
        "close_reason": VIEWER_REVOKED_REASON,
    }
    # The subject that was applied, so a caller can see its body was honoured.
    # Absent for a whole-session revoke, whose answer keeps its original shape.
    if subject is not None:
        answer["subject"] = subject
    return answer


async def _revoke_session(request: Request, session_id: str) -> float:
    """Record the cutoff for every share viewer of the session and return it."""
    p = principal(request)
    az = authz(request)
    # The embedder's service token is a global admin and revokes for everyone,
    # so only other callers are held to a per-principal share of the table.
    revoker = None if await az.is_admin(p) else p.subject_id
    evicted: list[str] = []
    try:
        revoked_at = az.revoke_share_viewers(session_id, revoker=revoker, on_evict=evicted.append)
    except ShareViewerRevocationsFullError:
        raise HTTPException(status_code=429, detail="too many sessions with revoked viewers; try again later") from None
    # The cap made room by dropping this principal's oldest cutoff, so that
    # session's earlier share tokens are admitted again: say so.
    for gone in evicted:
        audit_event(
            "session.viewers.cutoff_evicted",
            principal=p.subject_id,
            session_id=gone,
            detail={"revoked_session_id": session_id},
            source_ip=source_ip(request),
        )
    return revoked_at


def register_viewer_routes(router: APIRouter) -> None:
    """Bind the viewer-revocation route."""
    # The method comes from HttpMethod rather than a literal: FastAPI upper-cases
    # whatever it is given, so a "post" mutant of a literal would be equivalent.
    router.add_api_route(
        VIEWERS_REVOKE_PATH, revoke_viewers, methods=[HttpMethod.POST.value], name="sessions.viewers.revoke"
    )
