#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Application factory for the generic swarm manager."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from provide.telemetry import get_logger

from provide.terminal.manager.auth import setup_auth
from provide.terminal.manager.core import AgentManager
from provide.terminal.manager.process import AgentProcessManager
from provide.terminal.manager.routes import router as swarm_router

if TYPE_CHECKING:
    from provide.terminal.manager.config import ManagerConfig
    from provide.terminal.manager.models import AgentStatusBase
    from provide.terminal.manager.protocols import (
        AccountPoolPlugin,
        IdentityStorePlugin,
        ManagedAgentPlugin,
        StatusUpdatePlugin,
        TimeseriesPlugin,
        WorkerRegistryPlugin,
    )

logger = get_logger(__name__)


def create_manager_app(
    config: ManagerConfig,
    *,
    agent_status_class: type[AgentStatusBase] | None = None,
    worker_registry: dict[str, WorkerRegistryPlugin] | None = None,
    account_pool: AccountPoolPlugin | None = None,
    identity_store: IdentityStorePlugin | None = None,
    managed_agent: ManagedAgentPlugin | None = None,
    status_update: StatusUpdatePlugin | None = None,
    timeseries: TimeseriesPlugin | None = None,
    swarm_status_builder: Any | None = None,
    extra_routers: list[APIRouter] | None = None,
) -> tuple[FastAPI, AgentManager]:
    """Create a FastAPI application wired to a generic AgentManager.

    Returns ``(app, manager)`` so the caller can further customise
    either before starting the server.
    """
    manager = AgentManager(
        config,
        agent_status_class=agent_status_class,
        account_pool=account_pool,
        identity_store=identity_store,
        status_update=status_update,
        timeseries_plugin=timeseries,
        swarm_status_builder=swarm_status_builder,
    )

    process_mgr = AgentProcessManager(
        manager,
        worker_registry=worker_registry,
        log_dir=config.log_dir,
    )
    if config.spawn_policy_webhook_url:
        from provide.terminal.manager.ext import WebhookAgentSpawnPolicyGate

        process_mgr.set_policy_gate(
            WebhookAgentSpawnPolicyGate(
                url=config.spawn_policy_webhook_url,
                secret=config.spawn_policy_webhook_secret,
                timeout_s=config.spawn_policy_webhook_timeout_s,
            )
        )
    manager.agent_process_manager = process_mgr

    app = FastAPI(title=config.title)
    manager.app = app

    # Auth
    setup_auth(app, env_var=config.auth_token_env_var, config=config)

    # CORS
    cors_env = os.environ.get("UTERM_CORS_ORIGINS", "").strip()
    origins = [o.strip() for o in cors_env.split(",") if o.strip()] if cors_env else config.cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # Wire state
    app.state.swarm_manager = manager
    if managed_agent is not None:
        app.state.managed_agent_plugin = managed_agent

    # Include routes
    app.include_router(swarm_router)
    for extra in extra_routers or []:
        app.include_router(extra)

    # WebSocket endpoint for dashboard push updates
    @app.websocket("/ws/swarm")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        async with manager._ws_lock:
            manager.websocket_clients.add(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            async with manager._ws_lock:
                manager.websocket_clients.discard(websocket)
        except Exception as e:  # pragma: no cover — requires mid-stream WS failure
            logger.exception("websocket_error", error=str(e))
            async with manager._ws_lock:
                manager.websocket_clients.discard(websocket)

    # WebSocket endpoint for MCP client lifecycle tracking
    @app.websocket("/ws/mcp-client")
    async def mcp_client_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await manager.register_mcp_client(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            await manager.unregister_mcp_client(websocket)
        except Exception as e:  # pragma: no cover
            logger.exception("mcp_client_websocket_error", error=str(e))
            await manager.unregister_mcp_client(websocket)

    # Load persisted state
    manager._load_state()

    return app, manager
