#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Command dispatcher and built-in command handlers for ushell.

Commands
--------
help            — list all commands
clear           — erase the terminal screen
py <expr>       — evaluate a Python expression (or exec a statement)
sessions        — list active sessions from the KV registry
kv list         — list all KV keys with the session: prefix
kv get <key>    — read a KV value by key
fetch <url>     — HTTP GET (uses urllib; CF runtime uses js.fetch)
env             — show available context keys
exit / quit     — end the shell session
"""

from __future__ import annotations

from typing import Any

from provide.terminal.shell._output import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    PROMPT,
    RESET,
    YELLOW,
    error_msg,
    fmt_kv,
    fmt_table,
    heading,
    info_msg,
    success_msg,
    term,
)
from provide.terminal.shell._sandbox import Sandbox

# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

_HELP = (
    f"{heading('ushell commands')}"
    f"{fmt_kv('help', 'this help text')}"
    f"{fmt_kv('clear', 'erase the terminal screen')}"
    f"{fmt_kv('py <expr>', 'evaluate Python expression or statement')}"
    f"{fmt_kv('sessions', 'list active sessions from KV registry')}"
    f"{fmt_kv('kv list', 'list all KV session keys')}"
    f"{fmt_kv('kv get <key>', 'read a KV value')}"
    f"{fmt_kv('fetch <url>', 'HTTP GET request')}"
    f"{fmt_kv('env', 'show available context keys')}"
    f"{fmt_kv('exit / quit', 'end this shell session')}"
)

_KV_PREFIX = "session:"


class CommandDispatcher:
    """Parse and dispatch ushell command lines.

    Args:
        ctx:     Runtime context dict.  Expected optional keys:

                 ``list_kv_sessions``
                     Async callable ``() -> list[dict]`` — KV session list.
                 ``env``
                     CF env object with KV/DO bindings.

        sandbox: :class:`~provide.terminal.shell._sandbox.Sandbox` instance
                 for ``py`` commands.  A fresh one is created if omitted.
    """

    def __init__(self, ctx: dict[str, Any], sandbox: Sandbox | None = None) -> None:
        self._ctx = ctx
        self._sandbox = sandbox or Sandbox({"ctx": ctx})

    async def dispatch(self, line: str) -> list[dict[str, Any]]:
        """Process a completed *line* and return a list of ``term`` frames."""
        line = line.strip()
        # Ctrl+C — already echoed; just re-show prompt.
        if not line or line == "\x03":
            return [term(PROMPT)]

        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in {"exit", "quit", "\x04"}:
            return [term(info_msg("Goodbye.\r\n") + PROMPT)]

        if cmd == "help":
            return [term(_HELP + PROMPT)]

        if cmd == "clear":
            return [term("\x1b[2J\x1b[H" + PROMPT)]

        if cmd == "py":
            return await self._cmd_py(arg)

        if cmd == "sessions":
            return await self._cmd_sessions()

        if cmd == "kv":
            return await self._cmd_kv(arg)

        if cmd == "fetch":
            return await self._cmd_fetch(arg)

        if cmd == "env":
            return self._cmd_env()

        return [term(error_msg(f"unknown command: {cmd!r} — type {BOLD}help{RESET}") + PROMPT)]

    # ------------------------------------------------------------------
    # Command implementations
    # ------------------------------------------------------------------

    async def _cmd_py(self, source: str) -> list[dict[str, Any]]:
        if not source:
            return [term(error_msg("usage: py <expr>") + PROMPT)]
        result = self._sandbox.run(source)
        output = result if result else success_msg("ok")
        return [term(output + PROMPT)]

    async def _cmd_sessions(self) -> list[dict[str, Any]]:
        list_fn = self._ctx.get("list_kv_sessions")
        if list_fn is None:
            return [term(error_msg("list_kv_sessions not available in this context") + PROMPT)]
        try:
            sessions: list[dict[str, Any]] = await list_fn()
        except Exception as exc:
            return [term(error_msg(str(exc)) + PROMPT)]
        if not sessions:
            return [term(info_msg("no sessions found") + PROMPT)]
        rows: list[tuple[str, ...]] = [
            (
                str(s.get("session_id", "?")),
                str(s.get("lifecycle_state", "?")),
                str(s.get("connector_type", "?")),
                "live" if s.get("connected") else "idle",
            )
            for s in sessions
        ]
        table = fmt_table(rows, headers=("session_id", "state", "type", "status"))
        return [term(table + PROMPT)]

    async def _cmd_kv(self, arg: str) -> list[dict[str, Any]]:
        env = self._ctx.get("env")
        kv = getattr(env, "SESSION_REGISTRY", None) if env is not None else None
        if kv is None:
            return [term(error_msg("SESSION_REGISTRY KV binding not available") + PROMPT)]

        sub_parts = arg.split(None, 1)
        sub = sub_parts[0].lower() if sub_parts else ""
        key_arg = sub_parts[1].strip() if len(sub_parts) > 1 else ""

        if sub == "list":
            try:
                result = await kv.list(prefix=_KV_PREFIX)
                keys = result.keys if hasattr(result, "keys") else result.get("keys", [])
                names = [k.get("name") if isinstance(k, dict) else getattr(k, "name", str(k)) for k in keys]
                if not names:
                    return [term(info_msg("no keys found") + PROMPT)]
                lines = "\r\n".join(f"  {CYAN}{n}{RESET}" for n in names if n)
                return [term(lines + "\r\n" + PROMPT)]
            except Exception as exc:
                return [term(error_msg(str(exc)) + PROMPT)]

        if sub == "get":
            if not key_arg:
                return [term(error_msg("usage: kv get <key>") + PROMPT)]
            full_key = key_arg if key_arg.startswith(_KV_PREFIX) else _KV_PREFIX + key_arg
            try:
                value = await kv.get(full_key)
                if value is None:
                    return [term(info_msg(f"key not found: {full_key}") + PROMPT)]
                return [term(f"{DIM}{full_key}{RESET}\r\n{value}\r\n" + PROMPT)]
            except Exception as exc:
                return [term(error_msg(str(exc)) + PROMPT)]

        return [term(error_msg(f"usage: kv list | kv get <key>") + PROMPT)]  # noqa: F541

    async def _cmd_fetch(self, url: str) -> list[dict[str, Any]]:
        if not url:
            return [term(error_msg("usage: fetch <url>") + PROMPT)]
        try:
            # Try js.fetch (CF runtime) first, fall back to urllib.
            try:
                import js  # type: ignore[import-not-found]

                resp = await js.fetch(url)
                status = int(resp.status)
                body = await resp.text()
            except (ImportError, AttributeError):
                import urllib.request

                with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310  # nosec B310
                    status = r.status
                    body = r.read(4096).decode("utf-8", errors="replace")

            preview = body[:800].replace("\n", "\r\n")
            truncated = " …" if len(body) > 800 else ""
            color = GREEN if status < 400 else YELLOW if status < 500 else "\x1b[31m"
            return [term(f"{color}HTTP {status}{RESET}\r\n{preview}{truncated}\r\n" + PROMPT)]
        except Exception as exc:
            return [term(error_msg(str(exc)) + PROMPT)]

    def _cmd_env(self) -> list[dict[str, Any]]:
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
        return [term(output + PROMPT)]
