#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""ANSI output helpers for ushell.

All string constants use ``\\r\\n`` line endings (terminal protocol).
"""

from __future__ import annotations

import time
from typing import Any

# ---------------------------------------------------------------------------
# ANSI escape constants
# ---------------------------------------------------------------------------

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"

CLEAR_SCREEN = "\x1b[2J\x1b[H"

# ---------------------------------------------------------------------------
# Prompt and banner
# ---------------------------------------------------------------------------

PROMPT = f"{GREEN}❯{RESET} "  # noqa: RUF001

BANNER = (
    f"{BOLD}{CYAN}ushell{RESET} {DIM}— Python REPL inside your terminal{RESET}\r\n"
    f"{DIM}Type {RESET}help{DIM} for available commands.{RESET}\r\n\r\n"
)


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------


def term(data: str, ts: float | None = None) -> dict[str, Any]:
    """Build a ``term`` worker-protocol frame."""
    return {"type": "term", "data": data, "ts": ts or time.time()}


def worker_hello(input_mode: str = "open") -> dict[str, Any]:
    """Build a ``worker_hello`` frame declaring the session input mode."""
    return {"type": "worker_hello", "input_mode": input_mode, "ts": time.time()}


# ---------------------------------------------------------------------------
# Formatted message helpers (return strings, caller wraps in term())
# ---------------------------------------------------------------------------


def error_msg(text: str) -> str:
    return f"{RED}error:{RESET} {text}\r\n"


def info_msg(text: str) -> str:
    return f"{DIM}{text}{RESET}\r\n"


def success_msg(text: str) -> str:
    return f"{GREEN}{text}{RESET}\r\n"


def heading(text: str) -> str:
    return f"{BOLD}{CYAN}{text}{RESET}\r\n"


def fmt_kv(key: str, value: str, *, width: int = 20) -> str:
    return f"  {DIM}{key:<{width}}{RESET}{value}\r\n"


def fmt_table(rows: list[tuple[str, ...]], headers: tuple[str, ...] | None = None) -> str:
    """Format a list of tuples as a fixed-width table."""
    all_rows = list(rows)
    if not all_rows:
        return info_msg("(no results)")
    widths = [max(len(str(cell)) for cell in col) for col in zip(*all_rows, strict=False)]
    if headers:
        widths = [max(w, len(h)) for w, h in zip(widths, headers, strict=False)]
    lines: list[str] = []
    if headers:
        header_line = "  " + "  ".join(f"{BOLD}{h:<{w}}{RESET}" for h, w in zip(headers, widths, strict=False))
        lines.append(header_line)
        lines.append("  " + "  ".join("-" * w for w in widths))
    lines.extend("  " + "  ".join(f"{cell!s:<{w}}" for cell, w in zip(row, widths, strict=False)) for row in all_rows)
    return "\r\n".join(lines) + "\r\n"
