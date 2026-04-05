# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

# provide-terminal-manager

Generic agent swarm orchestration framework built on FastAPI. Manages the
lifecycle of agent processes -- spawning, health-checking, pausing, killing --
with WebSocket-based dashboard push updates, persistent state, time-series
metrics, and plugin slots for game-specific or domain-specific behaviour.

## Installation

```bash
pip install provide-terminal-manager[server]
```

Requires Python 3.11+. The `[server]` extra pulls in FastAPI, uvicorn, Pydantic,
and PyYAML.

## Application factory

```python
from provide.terminal.manager import create_manager_app, ManagerConfig

config = ManagerConfig(max_agents=20, port=8790)
app, manager = create_manager_app(config)
```

The returned `app` is a ready-to-run FastAPI instance; `manager` is the
`AgentManager` coordinator.

## Key classes

| Export | Description |
|---|---|
| `AgentManager` | Central coordinator: fleet ops, state persistence, WS broadcast |
| `ManagerConfig` | Pydantic settings: host, port, max_agents, log_dir, auth, auto-shutdown |
| `AgentStatusBase` | Base model for per-agent state (subclass for domain fields) |
| `SwarmStatus` | Snapshot of the full swarm: counts, uptime, time-series stats |
| `create_manager_app()` | Factory wiring config, plugins, routes, and auth |

## Plugin protocols

Game-specific behaviour is injected via typed protocol classes:

- `AccountPoolPlugin` -- assign/release accounts to agents
- `IdentityStorePlugin` -- persistent identity mapping
- `ManagedAgentPlugin` -- custom spawn/kill logic
- `StatusUpdatePlugin` -- enrich status updates
- `TimeseriesPlugin` -- custom metrics per sample
- `WorkerRegistryPlugin` -- map agents to worker processes

## Usage

```python
import asyncio
from provide.terminal.manager import create_manager_app, ManagerConfig

config = ManagerConfig(max_agents=10, port=8790, title="My Swarm")
app, manager = create_manager_app(config)
asyncio.run(manager.run())
```

## WebSocket endpoints

- `/ws/swarm` -- real-time swarm status push (dashboard)
- `/ws/mcp-client` -- MCP client lifecycle tracking (auto-shutdown)

## Links

- [Main repository README](../../README.md)
