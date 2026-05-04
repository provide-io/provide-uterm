#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Swarm status and timeseries API routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Depends

from provide.terminal.manager.routes.models import require_manager, router

if TYPE_CHECKING:
    from provide.terminal.manager.core import AgentManager


@router.get("/swarm/status")
async def status(manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    return manager.get_swarm_status().model_dump()


@router.get("/swarm/timeseries/info")
async def timeseries_info(manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    return manager.get_timeseries_info()


@router.get("/swarm/timeseries/recent")
async def timeseries_recent(limit: int = 200, manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    return {
        "rows": manager.get_timeseries_recent(limit=limit),
        "info": manager.get_timeseries_info(),
    }


@router.get("/swarm/timeseries/summary")
async def timeseries_summary(window_minutes: int = 120, manager: AgentManager = Depends(require_manager)) -> Any:  # noqa: B008
    return manager.get_timeseries_summary(window_minutes=window_minutes)
