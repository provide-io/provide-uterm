#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Durable Object storage commands."""

from __future__ import annotations

from typing import Any

from provide.terminal.shell._output import (
    CYAN,
    DIM,
    PROMPT,
    RESET,
    error_msg,
    info_msg,
)


async def cmd_storage(ctx: dict[str, Any], arg: str) -> list[str]:
    """Dispatch ``storage list|get`` subcommands against the DO storage."""
    storage = ctx.get("storage")
    if storage is None:
        return [error_msg("storage not available in this context") + PROMPT]

    sub_parts = arg.split(None, 1)
    sub = sub_parts[0].lower() if sub_parts else ""
    key_arg = sub_parts[1].strip() if len(sub_parts) > 1 else ""

    if sub == "list":
        try:
            result = await storage.list()
            keys_raw = result.keys if hasattr(result, "keys") else list(result)
            keys = [k.get("name") if isinstance(k, dict) else getattr(k, "name", str(k)) for k in keys_raw]
            if not keys:
                return [info_msg("no storage keys found") + PROMPT]
            lines = "\r\n".join(f"  {CYAN}{k}{RESET}" for k in keys if k)
            return [lines + "\r\n" + PROMPT]
        except Exception as exc:
            return [error_msg(str(exc)) + PROMPT]

    if sub == "get":
        if not key_arg:
            return [error_msg("usage: storage get <key>") + PROMPT]
        try:
            value = await storage.get(key_arg)
            if value is None:
                return [info_msg(f"key not found: {key_arg}") + PROMPT]
            return [f"{DIM}{key_arg}{RESET}\r\n{value}\r\n" + PROMPT]
        except Exception as exc:
            return [error_msg(str(exc)) + PROMPT]

    return [error_msg("usage: storage list | storage get <key>") + PROMPT]
