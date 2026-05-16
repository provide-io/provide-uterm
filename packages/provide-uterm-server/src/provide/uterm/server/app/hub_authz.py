#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Per-path capability gating for /worker/{id}/... REST routes.

The hub's REST router (``provide.uterm.bridge.hub.rest``) intentionally
carries no built-in authz; the protecting layer is this module.  Without it,
any authenticated principal — including a viewer-role share token — could
``POST /worker/{id}/hijack/acquire`` and seize control of a session.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from fastapi import HTTPException
from starlette.requests import HTTPConnection  # noqa: TC002

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from provide.uterm.server.authorization import AuthorizationService
    from provide.uterm.server.registry import SessionRegistry

# Regexes matching the hub-router REST paths that require capability checks.
HUB_WRITE_PATH = re.compile(
    r"^/worker/(?P<session_id>[\w\-]+)/"
    r"(?:hijack/(?:acquire|[\w\-]+/(?:send|step|heartbeat|release)))$"
)
HUB_MODE_PATH = re.compile(r"^/worker/(?P<session_id>[\w\-]+)/input_mode$")
HUB_ADMIN_PATH = re.compile(r"^/worker/(?P<session_id>[\w\-]+)/disconnect_worker$")
HUB_READ_PATH = re.compile(r"^/worker/(?P<session_id>[\w\-]+)/hijack/[\w\-]+/(?:snapshot|events)$")


def build_require_hub_route_authz(
    *,
    registry_getter: Callable[[], SessionRegistry | None],
) -> Callable[[HTTPConnection], Awaitable[None]]:
    """Return the FastAPI dependency that gates hub REST routes.

    ``registry_getter`` returns the current ``SessionRegistry`` (it is
    populated mid-construction in the factory, so the closure must read it
    lazily rather than capture an instance up-front).
    """

    async def _require_hub_route_authz(connection: HTTPConnection) -> None:
        """Gate /worker/{id}/... REST routes on session-level capabilities.

        Runs after _require_authenticated, so connection.state.uterm_principal
        is populated.  Applies only to REST paths served by the hub router;
        WebSocket routes handle their own per-session role resolution via
        _resolve_browser_role, so this dependency is a no-op for them.
        Uses HTTPConnection so the same dependency works for both the REST
        Request and WebSocket code paths FastAPI invokes.
        """
        path = str(connection.scope.get("path", ""))
        session_id: str | None = None
        required: str | None = None
        require_admin = False
        for pattern, cap in (
            (HUB_WRITE_PATH, "session.control.hijack"),
            (HUB_MODE_PATH, "session.control.mode"),
            (HUB_READ_PATH, "session.read"),
        ):
            m = pattern.match(path)
            if m is not None:
                session_id = m.group("session_id")
                required = cap
                break
        if session_id is None:
            m = HUB_ADMIN_PATH.match(path)
            if m is not None:
                session_id = m.group("session_id")
                require_admin = True
        if session_id is None:
            return  # Not a capability-gated hub route.
        principal = getattr(connection.state, "uterm_principal", None)
        if principal is None:  # pragma: no cover — _require_authenticated always sets this first
            raise HTTPException(status_code=401, detail="authentication required")
        authz_service = cast("AuthorizationService", connection.app.state.uterm_authz)
        if require_admin:
            if not await authz_service.is_admin(principal):
                raise HTTPException(status_code=403, detail="admin role required")
            return
        # ``required`` is bound by the matcher loop above whenever
        # ``require_admin`` is False and ``session_id`` is set; cast so mypy
        # can see the narrowing without an ``assert`` (which trips S101).
        cap_required = cast("str", required)
        registry = registry_getter()
        session = await registry.get_definition(session_id) if registry is not None else None
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        if cap_required == "session.read":
            if not await authz_service.can_read_session(principal, session):
                raise HTTPException(status_code=403, detail="insufficient privileges")
        else:
            if not await authz_service.can_mutate_session(principal, session, cap_required):
                raise HTTPException(status_code=403, detail="insufficient privileges")

    return _require_hub_route_authz
