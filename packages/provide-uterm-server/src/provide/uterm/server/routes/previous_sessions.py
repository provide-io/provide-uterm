#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Mutation-enforced with the rest of routes/ ([tool.mutmut]); bound suite:
#   tests/server/test_previous_sessions.py   the routes end to end, and the store behind them
"""Read the capture sessions that have ended (``session_persistence.previous_keep_days``), and set their owner.

Exposes:
  GET   /api/previous-sessions               -- the previous sessions the caller may read, newest first
  GET   /api/previous-sessions/{session_id}  -- one of them, with its last screen and scrollback
  PATCH /api/previous-sessions/{session_id}  -- set its ``owner`` and/or ``visibility`` (admins only)

A previous session is history, so it is read by fewer people than the live
session was: its owner, an admin, and an operator unless it was private. A
share viewer never, whatever its token: a watch grant is for the live stream,
and the cutoffs that revoke one are not kept across a restart. Everyone else
is answered as the live read answers a caller it refuses: an id nobody kept
is 404, and one the caller may not read is 403. What is returned passes
through the output-redaction policy, as the live screen does.

PAM creates a capture session with no owner, and whoever runs the login sets
one on the live session afterwards. A login that ends first leaves a previous
session nobody but an admin can read, so an admin may set its owner, and its
visibility with it, once it has ended: the live session's rule for an owner
change, which only an admin may make. A share viewer never, whatever its roles.

A runtime-native route rather than a shared ``RouteDef``: it reads files this
server keeps for its own PAM capture sessions, which the Cloudflare Worker has
neither of.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import ValidationError

from provide.uterm.server.audit import audit_event
from provide.uterm.server.capture_record import RecordError
from provide.uterm.server.config_schema_session import SessionDefinition
from provide.uterm.server.models import validation_error_message
from provide.uterm.server.routes._helpers import SessionId, authz, principal, source_ip

if TYPE_CHECKING:
    from provide.uterm.server.auth import Principal
    from provide.uterm.server.authorization import AuthorizationService
    from provide.uterm.server.capture_state import CaptureRecord, CaptureStateStore

#: The fields a PATCH may set: who owns the ended session, and who else may read it.
UPDATABLE_FIELDS = frozenset({"owner", "visibility"})

PREVIOUS_SESSIONS_PATH = "/previous-sessions"
PREVIOUS_SESSION_PATH = "/previous-sessions/{session_id}"


def _store(request: Request) -> CaptureStateStore | None:
    """The store behind these routes, or None when neither feature is on.

    A store that keeps no previous session has none to list: it writes none,
    and prunes at startup any an earlier configuration left.
    """
    store: CaptureStateStore | None = request.app.state.uterm_capture_state
    return store


def _unknown(session_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"unknown previous session: {session_id}")


@dataclass(frozen=True, slots=True)
class Standing:
    """What the caller is, whatever the session: asked of the provider once a request."""

    #: An administrator who may take over a session (``session.control.hijack``),
    #: globally; a session-scoped admin is admin of its own session only.
    admin: bool
    #: Admin of the one session its grant names, if any.
    admin_of: str | None
    operator: bool


async def caller_standing(p: Principal, az: AuthorizationService) -> Standing:
    """The caller's admin and operator standing, from the authorization provider.

    Asked once a request, not once a record: under a webhook provider each
    call is a round trip. Admin is the provider's ``is_admin`` (or, for a
    session-scoped grant, its session) with the take-over capability the live
    read's admin role requires; operator is the operator role, as the live
    read's role resolution reads it.
    """
    can_take_over = await az.has_capability(p, "session.control.hijack")
    scoped = p.admin_session_scope if "admin" in p.roles else None
    return Standing(
        admin=can_take_over and await az.is_admin(p),
        admin_of=scoped if can_take_over else None,
        operator=await az.has_role(p, "operator"),
    )


async def may_read_previous(p: Principal, az: AuthorizationService, standing: Standing, record: CaptureRecord) -> bool:
    """Whether *p* may read an ended session: its owner, its admin, or an operator of one not private.

    Never a share viewer (``share:...``, a watch or a tunnel share), and never
    anyone the live read would have refused: ``can_read_session`` and the
    owner check are the live read's per-session gate, and the rest is
    *standing* (:func:`caller_standing`).
    """
    definition = record.definition
    if p.subject_id.startswith("share:") or not await az.can_read_session(p, definition):
        return False
    if await az.is_owner(p, definition):
        return True
    if standing.admin or standing.admin_of == definition.session_id:
        return True
    return standing.operator and definition.visibility != "private"


async def list_previous_sessions(request: Request) -> dict[str, Any]:
    p = principal(request)
    az = authz(request)
    store = _store(request)
    kept = store.previous_sessions() if store is not None else []
    if not kept:
        return {"sessions": []}
    standing = await caller_standing(p, az)
    return {"sessions": [record.summary() for record in kept if await may_read_previous(p, az, standing, record)]}


async def get_previous_session(request: Request, session_id: SessionId) -> dict[str, Any]:
    p = principal(request)
    az = authz(request)
    store = _store(request)
    kept = store.previous_summary(session_id) if store is not None else None
    if store is None or kept is None:
        raise _unknown(session_id)
    # Decided on the listing's copy, before the file with the screen is read.
    if not await may_read_previous(p, az, await caller_standing(p, az), kept):
        raise HTTPException(status_code=403, detail="insufficient privileges")
    # None when the file has gone or turned unreadable since it was listed.
    record = await store.previous_session(session_id)
    if record is None:
        raise _unknown(session_id)
    try:
        screen, *scrollback = await request.app.state.uterm_hub.redact_texts_for_recipient(
            session_id, [str(record.screen), *record.scrollback], request
        )
    except Exception:
        # Fail closed: the unredacted screen is never the fallback.
        raise HTTPException(status_code=503, detail="output policy unavailable") from None
    return {**record.summary(), "screen": screen, "scrollback": scrollback}


def _changes(definition: SessionDefinition, payload: dict[str, Any]) -> dict[str, Any]:
    """*payload* once it is known to hold only an owner and a visibility the live session would take.

    The values are judged by the live session's own model, so a previous
    session takes exactly what a live one does. Its owner must be a string,
    though: one with none counts against the PAM user it names, which is where
    it started, so there is nothing to set it back to.
    """
    if not payload or not payload.keys() <= UPDATABLE_FIELDS:
        raise HTTPException(status_code=422, detail="a previous session takes owner and/or visibility, nothing else")
    if "owner" in payload and not isinstance(payload["owner"], str):
        raise HTTPException(status_code=422, detail="owner must be a string")
    try:
        SessionDefinition.model_validate({**definition.model_dump(mode="python"), **payload})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=validation_error_message(exc)) from None
    return payload


async def update_previous_session(
    request: Request, session_id: SessionId, payload: Annotated[dict[str, Any], Body(...)]
) -> dict[str, Any]:
    p = principal(request)
    az = authz(request)
    store = _store(request)
    kept = store.previous_summary(session_id) if store is not None else None
    if store is None or kept is None:
        raise _unknown(session_id)
    # Before the body is judged, as the live PATCH refuses before it validates.
    if p.subject_id.startswith("share:") or not await az.is_admin(p):
        raise HTTPException(status_code=403, detail="admin privileges required")
    changes = _changes(kept.definition, payload)
    try:
        record = await store.update_previous(session_id, changes)
    except RecordError:
        # The store has logged why; the old file and the listing are unchanged.
        raise HTTPException(status_code=500, detail="previous session not saved") from None
    if record is None:
        raise _unknown(session_id)
    audit_event(
        "previous_session.update",
        principal=p.subject_id,
        session_id=session_id,
        source_ip=source_ip(request),
        detail=changes,
    )
    return record.summary()


def register_previous_session_routes(router: APIRouter) -> None:
    """Bind the two previous-session reads and the owner change."""
    # No methods argument: add_api_route's own default is GET, so any value
    # written here that means GET (or none at all) would be an equivalent mutant.
    router.add_api_route(PREVIOUS_SESSIONS_PATH, list_previous_sessions, name="previous_sessions.list")
    router.add_api_route(PREVIOUS_SESSION_PATH, get_previous_session, name="previous_sessions.get")
    router.add_api_route(
        PREVIOUS_SESSION_PATH, update_previous_session, methods=["PATCH"], name="previous_sessions.update"
    )
