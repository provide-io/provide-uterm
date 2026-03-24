#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Command dispatcher and built-in command handlers for provide.shell.

Commands
--------
help [cmd]          — list all commands, or show detail for <cmd>
clear               — erase the terminal screen
py <expr>           — evaluate a Python expression (or exec a statement)
sessions            — list active sessions from the KV registry
sessions kill <id>  — force-terminate a session DO
kv list             — list all KV keys with the session: prefix
kv get <key>        — read a KV value by key
kv set <key> <val>  — write a KV entry
kv delete <key>     — delete a KV entry
fetch [-X METHOD] <url> [body] — HTTP request (GET by default)
storage list        — list DO storage keys
storage get <key>   — read a DO storage value
env                 — show available context keys
exit / quit         — end the shell session
"""

from __future__ import annotations

from typing import Any

from provide.shell._output import (
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
)
from provide.shell._sandbox import Sandbox

# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

_HELP = (
    f"{heading('ushell commands')}"
    f"{fmt_kv('help [cmd]', 'this help text, or detail for <cmd>')}"
    f"{fmt_kv('clear', 'erase the terminal screen')}"
    f"{fmt_kv('py <expr>', 'evaluate Python expression or statement')}"
    f"{fmt_kv('sessions', 'list active sessions from KV registry')}"
    f"{fmt_kv('sessions kill <id>', 'force-terminate a session DO')}"
    f"{fmt_kv('kv list', 'list all KV session keys')}"
    f"{fmt_kv('kv get <key>', 'read a KV value')}"
    f"{fmt_kv('kv set <key> <value>', 'write a KV entry')}"
    f"{fmt_kv('kv delete <key>', 'delete a KV entry')}"
    f"{fmt_kv('fetch [-X METHOD] <url> [body]', 'HTTP request (GET by default)')}"
    f"{fmt_kv('storage list', 'list DO storage keys')}"
    f"{fmt_kv('storage get <key>', 'read a DO storage value')}"
    f"{fmt_kv('env', 'show available context keys')}"
    f"{fmt_kv('exit / quit', 'end this shell session')}"
)

_COMMAND_HELP: dict[str, str] = {
    "help": "help [cmd] — show all commands or detail for <cmd>.\r\n",
    "clear": "clear — erase the terminal screen (ANSI reset).\r\n",
    "py": (
        "py <expr> — evaluate a Python expression or exec a statement.\r\n"
        "Variables persist across py calls for the session lifetime.\r\n"
        "Available: json, datetime, re, hashlib, base64, plus safe builtins.\r\n"
    ),
    "sessions": (
        "sessions — list all sessions from the KV registry.\r\n"
        "sessions kill <id> — force-terminate a session Durable Object.\r\n"
    ),
    "kv": (
        "kv list                  — list all KV keys with session: prefix.\r\n"
        "kv get <key>             — read a KV value (session: prefix added if absent).\r\n"
        "kv set <key> <value>     — write a KV entry.\r\n"
        "kv delete <key>          — delete a KV entry.\r\n"
    ),
    "fetch": (
        "fetch [-X METHOD] <url> [body] — HTTP request.\r\n"
        "  Default method is GET.  Use -X POST, -X PUT, etc. to change it.\r\n"
        "  Optional body is sent as the request body.\r\n"
    ),
    "storage": (
        "storage list         — list all DO storage keys.\r\nstorage get <key>    — read a DO storage value by key.\r\n"
    ),
    "env": "env — show available context keys and their types.\r\n",
    "exit": "exit / quit — end this shell session.\r\n",
    "quit": "exit / quit — end this shell session.\r\n",
}

_KV_PREFIX = "session:"


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

        sandbox: :class:`~provide.shell._sandbox.Sandbox` instance
                 for ``py`` commands.  A fresh one is created if omitted.
    """

    def __init__(self, ctx: dict[str, Any], sandbox: Sandbox | None = None) -> None:
        self._ctx = ctx
        self._sandbox = sandbox or Sandbox({"ctx": ctx})

    async def dispatch(self, line: str) -> list[str]:
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

        return [error_msg(f"unknown command: {cmd!r} — type {BOLD}help{RESET}") + PROMPT]

    # ------------------------------------------------------------------
    # Command implementations
    # ------------------------------------------------------------------

    async def _cmd_py(self, source: str) -> list[str]:
        if not source:
            return [error_msg("usage: py <expr>") + PROMPT]
        result = self._sandbox.run(source)
        output = result if result else success_msg("ok")
        return [output + PROMPT]

    async def _cmd_sessions(self) -> list[str]:
        list_fn = self._ctx.get("list_kv_sessions")
        if list_fn is None:
            return [error_msg("list_kv_sessions not available in this context") + PROMPT]
        try:
            sessions: list[dict[str, Any]] = await list_fn()
        except Exception as exc:
            return [error_msg(str(exc)) + PROMPT]
        if not sessions:
            return [info_msg("no sessions found") + PROMPT]
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
        return [table + PROMPT]

    async def _cmd_sessions_kill(self, session_id: str) -> list[str]:
        if not session_id:
            return [error_msg("usage: sessions kill <session_id>") + PROMPT]
        env = self._ctx.get("env")
        namespace = getattr(env, "SESSION_RUNTIME", None) if env is not None else None
        if namespace is None:
            return [error_msg("SESSION_RUNTIME DO binding not available") + PROMPT]
        try:
            stub_id = namespace.idFromName(session_id)
            stub = namespace.get(stub_id)

            class _FakeReq:
                method = "DELETE"
                # CF DO stub fetch — URL is routing-only, not a real network address.
                url = f"https://worker/api/sessions/{session_id}"

            await stub.fetch(_FakeReq())
            return [success_msg(f"kill signal sent to {session_id}") + PROMPT]
        except Exception as exc:
            return [error_msg(str(exc)) + PROMPT]

    async def _cmd_storage(self, arg: str) -> list[str]:
        storage = self._ctx.get("storage")
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

    async def _cmd_kv(self, arg: str) -> list[str]:
        env = self._ctx.get("env")
        kv = getattr(env, "SESSION_REGISTRY", None) if env is not None else None
        if kv is None:
            return [error_msg("SESSION_REGISTRY KV binding not available") + PROMPT]

        sub_parts = arg.split(None, 1)
        sub = sub_parts[0].lower() if sub_parts else ""
        key_arg = sub_parts[1].strip() if len(sub_parts) > 1 else ""

        if sub == "list":
            try:
                result = await kv.list(prefix=_KV_PREFIX)
                keys = result.keys if hasattr(result, "keys") else result.get("keys", [])
                names = [k.get("name") if isinstance(k, dict) else getattr(k, "name", str(k)) for k in keys]
                if not names:
                    return [info_msg("no keys found") + PROMPT]
                lines = "\r\n".join(f"  {CYAN}{n}{RESET}" for n in names if n)
                return [lines + "\r\n" + PROMPT]
            except Exception as exc:
                return [error_msg(str(exc)) + PROMPT]

        if sub == "get":
            if not key_arg:
                return [error_msg("usage: kv get <key>") + PROMPT]
            full_key = key_arg if key_arg.startswith(_KV_PREFIX) else _KV_PREFIX + key_arg
            try:
                value = await kv.get(full_key)
                if value is None:
                    return [info_msg(f"key not found: {full_key}") + PROMPT]
                return [f"{DIM}{full_key}{RESET}\r\n{value}\r\n" + PROMPT]
            except Exception as exc:
                return [error_msg(str(exc)) + PROMPT]

        if sub == "set":
            if not key_arg:
                return [error_msg("usage: kv set <key> <value>") + PROMPT]
            key_val_parts = key_arg.split(None, 1)
            if len(key_val_parts) < 2:
                return [error_msg("usage: kv set <key> <value>") + PROMPT]
            raw_key, value = key_val_parts
            full_key = raw_key if raw_key.startswith(_KV_PREFIX) else _KV_PREFIX + raw_key
            try:
                await kv.put(full_key, value)
                return [success_msg(f"set {full_key}") + PROMPT]
            except Exception as exc:
                return [error_msg(str(exc)) + PROMPT]

        if sub == "delete":
            if not key_arg:
                return [error_msg("usage: kv delete <key>") + PROMPT]
            full_key = key_arg if key_arg.startswith(_KV_PREFIX) else _KV_PREFIX + key_arg
            try:
                await kv.delete(full_key)
                return [success_msg(f"deleted {full_key}") + PROMPT]
            except Exception as exc:
                return [error_msg(str(exc)) + PROMPT]

        return [error_msg("usage: kv list | kv get <key> | kv set <key> <value> | kv delete <key>") + PROMPT]

    async def _cmd_fetch(self, arg: str) -> list[str]:
        if not arg:
            return [error_msg("usage: fetch [-X METHOD] <url> [body]") + PROMPT]

        method = "GET"
        rest = arg
        if rest == "-X" or rest.startswith(("-X ", "-X\t")):
            parts = rest[3:].lstrip().split(None, 1)
            if not parts:
                return [error_msg("usage: fetch [-X METHOD] <url> [body]") + PROMPT]
            method = parts[0].upper()
            rest = parts[1] if len(parts) > 1 else ""

        url_body = rest.split(None, 1)
        url = url_body[0] if url_body else ""
        body = url_body[1] if len(url_body) > 1 else None

        if not url:
            return [error_msg("usage: fetch [-X METHOD] <url> [body]") + PROMPT]

        try:
            try:
                import js  # type: ignore[import-not-found]

                opts: dict[str, object] = {"method": method}
                if body is not None:
                    opts["body"] = body
                resp = await js.fetch(url, opts)
                status = int(resp.status)
                text = await resp.text()
            except (ImportError, AttributeError):
                import urllib.request

                req = urllib.request.Request(url, method=method)  # noqa: S310  # nosec B310
                if body is not None:
                    req.data = body.encode("utf-8")
                with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310  # nosec B310
                    status = r.status
                    text = r.read(4096).decode("utf-8", errors="replace")

            preview = text[:800].replace("\n", "\r\n")
            truncated = " …" if len(text) > 800 else ""
            color = GREEN if status < 400 else YELLOW if status < 500 else "\x1b[31m"
            return [f"{color}HTTP {status}{RESET}\r\n{preview}{truncated}\r\n" + PROMPT]
        except Exception as exc:
            return [error_msg(str(exc)) + PROMPT]

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
