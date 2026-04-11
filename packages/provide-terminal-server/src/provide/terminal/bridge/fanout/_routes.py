#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""REST routes for the fan-out feature.

Registers:
- ``POST   /api/fanout/groups``                — create a fan-out group
- ``GET    /api/fanout/groups``                — list groups for the caller
- ``DELETE /api/fanout/groups/{group_id}``     — delete a group
- ``POST   /api/fanout/groups/{group_id}/send``   — broadcast input to a group
- ``POST   /api/fanout/groups/{group_id}/grants`` — grant access to another principal
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

try:
    from fastapi import APIRouter, Request  # noqa: TC002
    from fastapi.responses import JSONResponse
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for fanout routes: pip install 'provide-terminal[websocket]'") from _e

from provide.telemetry import get_logger
from provide.terminal.bridge.fanout._models import FanOutGroup
from provide.terminal.server.audit import audit_event

if TYPE_CHECKING:
    from provide.terminal.bridge.fanout._controller import FanOutController
    from provide.terminal.bridge.hub import TermHub

logger = get_logger(__name__)


def _get_controller(hub: TermHub) -> FanOutController:
    """Retrieve the FanOutController from the hub, raising 501 if absent."""
    ctrl: FanOutController | None = getattr(hub, "fan_out_controller", None)
    if ctrl is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=501, detail="fan-out feature is not enabled")
    return ctrl


def register_fanout_routes(hub: TermHub, router: APIRouter) -> None:
    """Attach fan-out REST routes to *router*."""

    @router.post("/api/fanout/groups")
    async def create_group(request: Request) -> Any:
        ctrl = _get_controller(hub)
        principal = request.state.uterm_principal
        body = await request.json()
        worker_ids = body.get("worker_ids", [])
        name = body.get("name", "")
        mode = body.get("mode", "parallel")
        stop_on_first_error = body.get("stop_on_first_error", False)
        error_pattern = body.get("error_pattern")
        quiesce_ms = body.get("quiesce_ms", 500)
        max_response_ms = body.get("max_response_ms", 10_000)
        divergence_threshold = body.get("divergence_threshold", 0.8)

        group = FanOutGroup(
            group_id=uuid.uuid4().hex,
            name=name,
            worker_ids=list(worker_ids),
            created_by=principal.subject_id,
            created_at=time.time(),
            mode=mode,
            stop_on_first_error=stop_on_first_error,
            error_pattern=error_pattern,
            quiesce_ms=quiesce_ms,
            max_response_ms=max_response_ms,
            divergence_threshold=divergence_threshold,
        )
        try:
            group_id = await ctrl.create_group(group, principal=principal.subject_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        audit_event("fanout.create_group", principal=principal.subject_id, detail={"group_id": group_id, "name": name})
        logger.info("fanout_group_created group_id=%s principal=%s", group_id, principal.subject_id)
        return {"group_id": group_id, "name": name, "session_count": len(worker_ids)}

    @router.get("/api/fanout/groups")
    async def list_groups(request: Request) -> Any:
        ctrl = _get_controller(hub)
        principal = request.state.uterm_principal
        groups = await ctrl.list_groups(principal.subject_id)
        return [
            {"group_id": g.group_id, "name": g.name, "session_count": len(g.worker_ids), "mode": g.mode}
            for g in groups
        ]

    @router.delete("/api/fanout/groups/{group_id}")
    async def delete_group(request: Request, group_id: str) -> Any:
        ctrl = _get_controller(hub)
        principal = request.state.uterm_principal
        existing = await ctrl.get_group(group_id, principal=principal.subject_id)
        if existing is None:
            return JSONResponse({"error": "group not found"}, status_code=404)
        if existing.created_by != principal.subject_id:
            return JSONResponse({"error": "only the group creator can delete it"}, status_code=403)
        await ctrl.delete_group(group_id, principal=principal.subject_id)
        audit_event("fanout.delete_group", principal=principal.subject_id, detail={"group_id": group_id})
        logger.info("fanout_group_deleted group_id=%s principal=%s", group_id, principal.subject_id)
        return JSONResponse(status_code=204, content=None)

    @router.post("/api/fanout/groups/{group_id}/send")
    async def send_to_group(request: Request, group_id: str) -> Any:
        ctrl = _get_controller(hub)
        principal = request.state.uterm_principal
        existing = await ctrl.get_group(group_id, principal=principal.subject_id)
        if existing is None:
            return JSONResponse({"error": "group not found"}, status_code=404)
        body = await request.json()
        data = body.get("data", "")
        quiesce_ms = body.get("quiesce_ms")
        max_response_ms = body.get("max_response_ms")
        result = await ctrl.send(
            group_id,
            data,
            principal=principal.subject_id,
            quiesce_ms=quiesce_ms,
            max_response_ms=max_response_ms,
        )
        audit_event(
            "fanout.send",
            principal=principal.subject_id,
            detail={"group_id": group_id, "send_id": result.send_id, "command": data[:120]},
        )
        logger.info("fanout_send group_id=%s send_id=%s principal=%s", group_id, result.send_id, principal.subject_id)
        return {
            "group_id": result.group_id,
            "send_id": result.send_id,
            "command": result.command,
            "sent_at": result.sent_at,
            "results": [asdict(r) for r in result.results],
            "divergent_sessions": result.divergent_sessions,
            "failed_sessions": result.failed_sessions,
        }

    @router.post("/api/fanout/groups/{group_id}/grants")
    async def grant_access(request: Request, group_id: str) -> Any:
        ctrl = _get_controller(hub)
        principal = request.state.uterm_principal
        existing = await ctrl.get_group(group_id, principal=principal.subject_id)
        if existing is None:
            return JSONResponse({"error": "group not found"}, status_code=404)
        if existing.created_by != principal.subject_id:
            return JSONResponse({"error": "only the group creator can grant access"}, status_code=403)
        body = await request.json()
        grantee = body.get("grantee", "")
        await ctrl.grant_access(group_id, grantee, principal=principal.subject_id)
        audit_event(
            "fanout.grant_access",
            principal=principal.subject_id,
            detail={"group_id": group_id, "grantee": grantee},
        )
        logger.info("fanout_grant group_id=%s grantee=%s principal=%s", group_id, grantee, principal.subject_id)
        return JSONResponse(status_code=204, content=None)
