#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""CLI entry point for provide.shell — run with: python -m provide.shell"""

from __future__ import annotations

import asyncio
import sys

from provide.shell._commands import CommandDispatcher
from provide.shell._output import BANNER, PROMPT


async def _cli() -> None:
    dispatcher = CommandDispatcher({})
    sys.stdout.write(BANNER.replace("\r\n", "\n"))
    sys.stdout.write(PROMPT.replace("\r\n", "\n"))
    sys.stdout.flush()
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.rstrip("\n\r")
        except (EOFError, KeyboardInterrupt):
            break
        results = await dispatcher.dispatch(line)
        for text in results:
            sys.stdout.write(text.replace("\r\n", "\n"))
        sys.stdout.flush()
        if any("Goodbye" in r for r in results):
            break


def main() -> None:
    asyncio.run(_cli())


if __name__ == "__main__":
    main()
