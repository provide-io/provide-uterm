#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""CLI entry point for provide.terminal.shell — run with: python -m provide.terminal.shell"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from provide.terminal.shell._commands import AnimatedResult, CommandDispatcher
from provide.terminal.shell._output import BANNER, PROMPT


async def _play_animation(result: AnimatedResult, write: Any) -> None:
    """Stream animation frames to *write* with timing."""
    delay = 1.0 / result.fps if result.fps > 0 else 0.1
    try:
        while True:
            for i, frame in enumerate(result.frames):
                text = frame
                if not result.loop and i == len(result.frames) - 1:
                    text += PROMPT.replace("\r\n", "\n")
                write(text.replace("\r\n", "\n"))
                sys.stdout.flush()
                await asyncio.sleep(delay)
            if not result.loop:
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        write("\r\n" + PROMPT.replace("\r\n", "\n"))
        sys.stdout.flush()


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
        result = await dispatcher.dispatch(line)
        if isinstance(result, AnimatedResult):
            await _play_animation(result, sys.stdout.write)
        else:
            for text in result:
                sys.stdout.write(text.replace("\r\n", "\n"))
            sys.stdout.flush()
        if isinstance(result, list) and any("Goodbye" in r for r in result):
            break


def main() -> None:
    asyncio.run(_cli())


if __name__ == "__main__":
    main()
