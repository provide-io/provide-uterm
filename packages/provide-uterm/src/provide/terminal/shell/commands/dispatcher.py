#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Top-level :class:`CommandDispatcher` that routes ushell command lines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from provide.terminal.shell._output import (
    BOLD,
    PROMPT,
    RESET,
    error_msg,
    fmt_kv,
    heading,
    info_msg,
)
from provide.terminal.shell._sandbox import Sandbox
from provide.terminal.shell.commands.cast import cmd_cast
from provide.terminal.shell.commands.fetch import cmd_fetch
from provide.terminal.shell.commands.help import _COMMAND_HELP, _HELP
from provide.terminal.shell.commands.kv import cmd_kv, cmd_sessions, cmd_sessions_kill
from provide.terminal.shell.commands.py import cmd_py
from provide.terminal.shell.commands.render import cmd_render
from provide.terminal.shell.commands.storage import cmd_storage

if TYPE_CHECKING:
    from provide.terminal.shell.commands.types import AnimatedResult


class CommandDispatcher:
    """Parse and dispatch ushell command lines.

    Args:
        ctx:     Runtime context dict.  Expected optional keys:

                 ``list_kv_sessions``
                     Async callable ``() -> list[dict]`` — KV session list.
                 ``env``
                     CF env object with KV/DO bindings.
                 ``storage``
                     DO storage object (ctx.storage).

        sandbox: :class:`~provide.terminal.shell._sandbox.Sandbox` instance
                 for ``py`` commands.  A fresh one is created if omitted.
    """

    def __init__(self, ctx: dict[str, Any], sandbox: Sandbox | None = None) -> None:
        self._ctx = ctx
        self._sandbox = sandbox or Sandbox({"ctx": ctx})

    async def dispatch(self, line: str) -> list[str] | AnimatedResult:
        """Process a completed *line* and return a list of raw output strings."""
        line = line.strip()
        # Ctrl+C — already echoed; just re-show prompt.
        if not line or line == "\x03":
            return [PROMPT]

        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in {"exit", "quit", "\x04"}:
            return [info_msg("Goodbye.\r\n") + PROMPT]

        if cmd == "help":
            if arg:
                detail = _COMMAND_HELP.get(arg.lower())
                if detail is None:
                    return [error_msg(f"no help for {arg!r}") + PROMPT]
                return [detail + PROMPT]
            return [_HELP + PROMPT]

        if cmd == "clear":
            return ["\x1b[2J\x1b[H" + PROMPT]

        if cmd == "py":
            return await self._cmd_py(arg)

        if cmd == "sessions":
            if arg.startswith("kill ") or arg == "kill":
                return await self._cmd_sessions_kill(arg[5:].strip() if arg.startswith("kill ") else "")
            return await self._cmd_sessions()

        if cmd == "kv":
            return await self._cmd_kv(arg)

        if cmd == "fetch":
            return await self._cmd_fetch(arg)

        if cmd == "storage":
            return await self._cmd_storage(arg)

        if cmd == "env":
            return self._cmd_env()

        if cmd == "render":
            return await self._cmd_render(arg)

        if cmd == "cast":
            return await self._cmd_cast(arg)

        return [error_msg(f"unknown command: {cmd!r} — type {BOLD}help{RESET}") + PROMPT]

    # ------------------------------------------------------------------
    # Command implementations — thin wrappers around topical submodules
    # ------------------------------------------------------------------

    async def _cmd_py(self, source: str) -> list[str]:
        return await cmd_py(self._sandbox, source)

    async def _cmd_sessions(self) -> list[str]:
        return await cmd_sessions(self._ctx)

    async def _cmd_sessions_kill(self, session_id: str) -> list[str]:
        return await cmd_sessions_kill(self._ctx, session_id)

    async def _cmd_storage(self, arg: str) -> list[str]:
        return await cmd_storage(self._ctx, arg)

    async def _cmd_kv(self, arg: str) -> list[str]:
        return await cmd_kv(self._ctx, arg)

    async def _cmd_fetch(self, arg: str) -> list[str]:
        return await cmd_fetch(arg)

    def _cmd_env(self) -> list[str]:
        env = self._ctx.get("env")
        lines: list[str] = []
        if env is not None:
            for attr in sorted(dir(env)):
                if attr.startswith("_"):
                    continue
                lines.append(fmt_kv(attr, type(getattr(env, attr, None)).__name__))
        else:
            ctx_keys = sorted(str(k) for k in self._ctx if not str(k).startswith("_"))
            lines = [fmt_kv(k, "") for k in ctx_keys]
        output = heading("context") + "".join(lines) if lines else info_msg("(empty context)")
        return [output + PROMPT]

    async def _cmd_render(self, arg: str) -> list[str] | AnimatedResult:
        return await cmd_render(arg)

    async def _cmd_cast(self, arg: str) -> list[str] | AnimatedResult:
        return await cmd_cast(arg)
