#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Generic swarm manager for agent orchestration.

Public API::

    from provide.terminal.manager import create_manager_app, ManagerConfig, AgentManager
    from provide.terminal.manager.models import AgentStatusBase, SwarmStatus, SpawnBatchRequest
    from provide.terminal.manager.protocols import (
        AccountPoolPlugin, IdentityStorePlugin, ManagedAgentPlugin,
        StatusUpdatePlugin, TimeseriesPlugin, WorkerRegistryPlugin,
    )

    app, manager = create_manager_app(ManagerConfig(), worker_registry={...})
"""

from __future__ import annotations

from provide.terminal.manager.app import create_manager_app
from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.core import AgentManager
from provide.terminal.manager.models import AgentStatusBase, SwarmStatus

__all__ = [
    "AgentManager",
    "AgentStatusBase",
    "ManagerConfig",
    "SwarmStatus",
    "create_manager_app",
]
