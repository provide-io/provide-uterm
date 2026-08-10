#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastAPI adapter for shared PAM event ingestion.

Mutation-enforced at killed==100 (217/217, no documented equivalents); bound
suite: tests/server/test_routes_pam_events_mutation_killing.py. It measured
6.91% until 2026-08-09: 9bc4dd0c moved ``ingest`` out of its ``@router.post``
decorator into the undecorated factory below, and mutmut skips decorated
functions, so every literal here became mutable at once behind tests that
already had 100% line coverage.

Two invariants the suite exists to protect:

* ``_tty_slug`` is identity-critical. The session id is *derived*
  (``pam-{username}-{slug}``), never supplied, so any change to the slug rules
  can make two different ttys collide onto one id — one login's observer
  session then adopts another's, and a ``close`` deletes the wrong one.
* ``authorize_pam_event_roles`` resolves ``admin`` through the authorization
  service but matches every other role against the principal's own claim set.
  Those branches are not interchangeable: swapping them either asks the service
  about a role it cannot answer, or accepts a self-asserted ``admin`` claim.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from provide.uterm.api_routes import API_ROUTES, RouteDef
from provide.uterm.server.registry import SessionValidationError
from provide.uterm.server.routes._helpers import authz, principal, registry
from provide.uterm.server.routes.route_defs import bind_api_routes

if TYPE_CHECKING:
    from collections.abc import Callable

_TTY_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def _tty_slug(tty: str) -> str:
    """Derive the Cloudflare-compatible terminal slug from *tty*."""
    basename = tty.split("/")[-1] if "/" in tty else tty
    return _TTY_SLUG_RE.sub("-", basename).strip("-") or "tty"


async def authorize_pam_event_roles(request: Request, required_roles: tuple[str, ...]) -> bool:
    """Allow either operator or admin for PAM RouteDef role alternatives."""
    p = principal(request)
    az = authz(request)
    for role in required_roles:
        if role == "admin":
            if await az.is_admin(p):
                return True
        elif role in p.roles:
            return True
    return False


def pam_event_capability_handlers() -> dict[str, Callable[..., object]]:
    """Return the FastAPI handler for the shared ``pam_events.ingest`` capability."""

    # Deliberately ``Any`` rather than ``JSONResponse | dict[str, Any]``, which
    # is what it answers with: FastAPI reads a handler's return annotation to
    # build a response model, and a union of a Response and a mapping is not a
    # Pydantic field. Annotating the union refuses the *whole application* at
    # registration time — ``create_server_app`` raises FastAPIError and no
    # route is served, which is the state main was left in by 1e146fe8.
    async def ingest(request: Request) -> Any:
        p = principal(request)
        if not await authz(request).can_create_session(p):
            raise HTTPException(status_code=403, detail="insufficient privileges")

        try:
            raw = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(raw, dict):
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        body: dict[str, Any] = raw

        event = str(body.get("event") or "")
        if event not in {"open", "close"}:
            return JSONResponse({"error": "unknown_event", "event": event}, status_code=422)
        username = str(body.get("username") or "")
        if not username:
            return JSONResponse({"error": "missing_username"}, status_code=422)

        tty = str(body.get("tty") or "")
        session_id = f"pam-{username}-{_tty_slug(tty)}"
        if event == "close":
            await registry(request).delete_session(session_id)
            return {"ok": True, "session_id": session_id, "action": "deleted"}

        # This endpoint is the relay destination: create only the local observer
        # session.  Calling pam_integration._on_open here would forward the event
        # to relay_url again and form a relay loop.
        payload: dict[str, object] = {
            "session_id": session_id,
            "display_name": f"{username} ({tty or 'pam'})",
            "connector_type": "shell",
            "connector_config": {},
            "input_mode": "open",
            "auto_start": False,
            "ephemeral": True,
            "tags": ["pam", str(body.get("mode") or "notify"), username],
            "recording_enabled": False,
            "owner": username,
            "visibility": "operator",
        }
        try:
            await registry(request).create_session(payload)
        except SessionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            # Cloudflare KV ``put`` overwrites an existing entry; retain its
            # idempotent open-event response when the observer already exists.
            if await registry(request).get_definition(session_id) is None:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "session_id": session_id, "action": "created"}

    return {"pam_events.ingest": ingest}


async def _unregistered_capability_handler() -> None:
    """Satisfy complete shared-capability validation for unbound routes."""
    raise RuntimeError("unregistered shared API capability invoked")


def register_pam_event_routes(router: APIRouter) -> None:
    """Bind the shared PAM event route exactly once through its RouteDef."""
    pam_handlers = pam_event_capability_handlers()
    handlers: dict[str, Callable[..., object]] = {
        route.capability: _unregistered_capability_handler for route in API_ROUTES
    }
    handlers.update(pam_handlers)
    selected: tuple[RouteDef, ...] = tuple(route for route in API_ROUTES if route.capability in pam_handlers)
    # Unlike the server's other shared definitions, this global RouteDef
    # already includes its ``/api`` prefix to match the Cloudflare worker. Bind
    # it on an unprefixed child before transferring the concrete route.
    pam_router = APIRouter()
    bind_api_routes(pam_router, handlers, selected, role_authorizer=authorize_pam_event_roles)
    router.routes.extend(pam_router.routes)
