#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Generic swarm manager for agent orchestration.

Public API::

    from provide.uterm.manager import create_manager_app, ManagerConfig, AgentManager
    from provide.uterm.manager.models import AgentStatusBase, SwarmStatus, SpawnBatchRequest
    from provide.uterm.manager.protocols import (
        AccountPoolPlugin, IdentityStorePlugin, ManagedAgentPlugin,
        StatusUpdatePlugin, TimeseriesPlugin, WorkerRegistryPlugin,
    )

    app, manager = create_manager_app(ManagerConfig(), worker_registry={...})
"""

from __future__ import annotations

from provide.uterm.manager.app import create_manager_app
from provide.uterm.manager.config import ManagerConfig
from provide.uterm.manager.core import AgentManager
from provide.uterm.manager.models import AgentStatusBase, SwarmStatus

__all__ = [
    "AgentManager",
    "AgentStatusBase",
    "ManagerConfig",
    "SwarmStatus",
    "create_manager_app",
]
