#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared dependency providers and router for swarm manager routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, HTTPException, Request

if TYPE_CHECKING:
    from provide.terminal.manager.core import AgentManager
    from provide.terminal.manager.protocols import AccountPoolPlugin, IdentityStorePlugin, ManagedAgentPlugin

router = APIRouter()


def require_manager(request: Request) -> AgentManager:
    """FastAPI dependency that returns the AgentManager or raises 503."""
    manager = getattr(request.app.state, "swarm_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Swarm manager not initialized")
    return cast("AgentManager", manager)


def get_identity_store(request: Request) -> IdentityStorePlugin | None:
    """Return the manager-owned identity store (or None)."""
    manager = require_manager(request)
    return manager.identity_store


def get_account_pool(request: Request) -> AccountPoolPlugin | None:
    """Return the manager-owned account pool (or None)."""
    manager = require_manager(request)
    return manager.account_pool


def get_managed_agent_plugin(request: Request) -> ManagedAgentPlugin | None:
    """Return the managed-agent plugin (or None)."""
    return cast("ManagedAgentPlugin | None", getattr(request.app.state, "managed_agent_plugin", None))
