# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

# provide-terminal-ai

MCP (Model Context Protocol) server that exposes the full provide-terminal control
plane as tool calls. Allows AI agents to manage terminal sessions, acquire hijack
leases, send keystrokes, read screen snapshots, and subscribe to real-time events
-- all over the standard MCP stdio transport.

## Installation

```bash
pip install provide-terminal-ai[mcp]
```

Requires Python 3.11+. The `[mcp]` extra pulls in `fastmcp>=2.0`.

## CLI

The package ships a `uterm-mcp` entry point:

```bash
uterm-mcp --url http://localhost:8780
uterm-mcp --url http://localhost:8780 --entity-prefix /agent
uterm-mcp --url http://localhost:8780 --header Authorization:"Bearer tok"
```

## Tools (18 total)

| Category | Tools |
|---|---|
| Hijack lifecycle | `hijack_begin`, `hijack_heartbeat`, `hijack_read`, `hijack_send`, `hijack_step`, `hijack_release` |
| Session management | `session_list`, `session_status`, `session_read`, `session_create`, `session_connect`, `session_disconnect`, `session_set_mode` |
| Server / worker | `server_health`, `worker_input_mode`, `worker_disconnect` |
| Event subscription | `session_watch`, `session_subscribe` |

## Usage

```python
from provide.terminal.ai import create_mcp_app

app = create_mcp_app("http://localhost:8780")
app.run(transport="stdio")
```

Or with custom headers and entity prefix:

```python
app = create_mcp_app(
    "http://localhost:8780",
    entity_prefix="/agent",
    headers={"Authorization": "Bearer tok"},
)
```

## Key modules

- `provide.terminal.ai.server` -- `create_mcp_app()` factory, all 18 tool registrations
- `provide.terminal.ai.cli` -- `uterm-mcp` argument parser and entry point

## Links

- [Main repository README](../../README.md)
