#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared helpers for server API route modules."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import HTTPException, Path, Request

if TYPE_CHECKING:
    from provide.uterm.server.auth import Principal
    from provide.uterm.server.authorization import AuthorizationService
    from provide.uterm.server.models import SessionDefinition
    from provide.uterm.server.registry import SessionRegistry

# Validated session_id path parameter -- rejects path-unsafe characters.
SessionId = Annotated[str, Path(pattern=r"^[\w\-]+$")]


def set_span_attrs(span: Any, **attrs: str | None) -> None:
    """Set uterm.* attributes on a span if it exposes set_attribute."""
    set_attr = getattr(span, "set_attribute", None)
    if not callable(set_attr):
        return
    mapping = {
        "session_id": "uterm.session_id",
        "worker_id": "uterm.worker_id",
        "operation": "uterm.operation",
        "principal": "uterm.principal",
        "http_method": "http.method",
        "http_path": "http.target",
    }
    for key, otel_key in mapping.items():
        val = attrs.get(key)
        if val is not None:
            set_attr(otel_key, val)


def registry(request: Request) -> SessionRegistry:
    return cast("SessionRegistry", request.app.state.uterm_registry)


def authz(request: Request) -> AuthorizationService:
    return cast("AuthorizationService", request.app.state.uterm_authz)


def principal(request: Request) -> Principal:
    p = getattr(request.state, "uterm_principal", None)
    if p is None:
        raise HTTPException(status_code=500, detail="principal was not resolved")
    return cast("Principal", p)


def source_ip(request: Request) -> str:
    return str(getattr(request.client, "host", "unknown")) if request.client else "unknown"


async def session_definition(request: Request, session_id: str) -> SessionDefinition:
    session = await registry(request).get_definition(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
    return session


def sid_not_found(session_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"unknown session: {session_id}")
