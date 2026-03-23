#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""CLI entry point for a standalone swarm manager (uterm-manager)."""

from __future__ import annotations

import asyncio
import sys


def main() -> None:
    """Run a bare swarm manager without game plugins."""
    from provide.terminal.manager.app import create_manager_app
    from provide.terminal.manager.config import ManagerConfig

    config = ManagerConfig()

    # Simple CLI arg parsing (--host, --port, --log-level)
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--host" and i + 1 < len(args):
            config.host = args[i + 1]
            i += 2
        elif args[i] == "--port" and i + 1 < len(args):
            config.port = int(args[i + 1])
            i += 2
        elif args[i] == "--log-level" and i + 1 < len(args):
            config.log_level = args[i + 1]
            i += 2
        else:
            i += 1

    _app, manager = create_manager_app(config)
    asyncio.run(manager.run())


if __name__ == "__main__":  # pragma: no cover
    main()
