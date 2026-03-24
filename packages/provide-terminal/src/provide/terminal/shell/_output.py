#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shim: re-exports from provide.shell._output and provide.shell.terminal._output."""

from provide.shell._output import (  # type: ignore[import-not-found]
    BANNER,
    BLUE,
    BOLD,
    CLEAR_SCREEN,
    CYAN,
    DIM,
    GREEN,
    MAGENTA,
    PROMPT,
    RED,
    RESET,
    YELLOW,
    error_msg,
    fmt_kv,
    fmt_table,
    heading,
    info_msg,
    success_msg,
)
from provide.shell.terminal._output import term, worker_hello  # type: ignore[import-not-found]

__all__ = [
    "BANNER",
    "BLUE",
    "BOLD",
    "CLEAR_SCREEN",
    "CYAN",
    "DIM",
    "GREEN",
    "MAGENTA",
    "PROMPT",
    "RED",
    "RESET",
    "YELLOW",
    "error_msg",
    "fmt_kv",
    "fmt_table",
    "heading",
    "info_msg",
    "success_msg",
    "term",
    "worker_hello",
]
