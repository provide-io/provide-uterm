#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""REST worker-control routes for the hijack hub.

These endpoints sit alongside the hijack-lease routes in
:mod:`provide.uterm.server.bridge.routes.rest` but operate on the worker as a
whole rather than on a specific hijack session.

Registers:
- ``POST /worker/{id}/input_mode``        — switch a worker between ``hijack`` and ``open`` input modes
- ``POST /worker/{id}/disconnect_worker`` — forcibly drop a worker's connection

.. rubric:: Authentication

Like the hijack routes, these endpoints have **no built-in authentication**.
Protect the router at the application layer before exposing it to untrusted
clients — see the :mod:`~provide.uterm.server.bridge.routes.rest` module
docstring for guidance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:
    from fastapi import APIRouter, Body, Path
    from fastapi.responses import JSONResponse
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for hijack routes: pip install 'provide-uterm[websocket]'") from _e

from provide.telemetry import get_logger

# Runtime import (not TYPE_CHECKING): FastAPI resolves this annotation at
# request time via ``get_type_hints`` to drive body validation.
from provide.uterm.server.bridge.models import InputModeRequest  # noqa: TC001

if TYPE_CHECKING:
    from provide.uterm.server.bridge.hub import TermHub

logger = get_logger(__name__)


def register_workerctl_routes(hub: TermHub, router: APIRouter) -> None:
    """Attach worker-control REST routes to *router*.

    .. warning::
        No authentication is applied.  Callers are responsible for protecting
        the router before exposing it to untrusted clients — see the
        :mod:`~provide.uterm.server.bridge.routes.rest` module docstring.
    """

    @router.post("/worker/{worker_id}/input_mode")
    async def set_input_mode(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        request: InputModeRequest = Body(...),  # noqa: B008
    ) -> Any:
        # input_mode is validated by Pydantic to be "hijack" or "open" via the
        # regex pattern on InputModeRequest, so the cast is sound.
        ok, err = await hub.set_input_mode(worker_id, request.input_mode)  # type: ignore[arg-type]  # ty:ignore[invalid-argument-type]
        if not ok:
            status = 404 if err == "not_found" else 409
            error_msg = (
                "No worker registered." if err == "not_found" else "Cannot switch to open while hijack is active."
            )
            logger.warning("rest_input_mode_error worker_id=%s mode=%s err=%s", worker_id, request.input_mode, err)
            return JSONResponse({"error": error_msg}, status_code=status)
        logger.info("rest_input_mode_ok worker_id=%s mode=%s", worker_id, request.input_mode)
        return {"ok": True, "input_mode": request.input_mode, "worker_id": worker_id}

    @router.post("/worker/{worker_id}/disconnect_worker")
    async def disconnect_worker(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
    ) -> Any:
        ok = await hub.disconnect_worker(worker_id)
        if not ok:
            logger.warning("rest_disconnect_no_worker worker_id=%s", worker_id)
            return JSONResponse({"error": "No worker connected."}, status_code=404)
        logger.info("rest_disconnect_ok worker_id=%s", worker_id)
        return {"ok": True, "worker_id": worker_id}
