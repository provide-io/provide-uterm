#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""KV-registry and session-listing commands."""

from __future__ import annotations

from typing import Any

from provide.uterm.shell._output import (
    CYAN,
    DIM,
    PROMPT,
    RESET,
    error_msg,
    fmt_table,
    info_msg,
    success_msg,
)

_KV_PREFIX = "session:"


async def cmd_sessions(ctx: dict[str, Any]) -> list[str]:
    """List sessions stored in the KV registry."""
    list_fn = ctx.get("list_kv_sessions")
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


async def cmd_sessions_kill(ctx: dict[str, Any], session_id: str) -> list[str]:
    """Force-terminate a session Durable Object via DELETE."""
    if not session_id:
        return [error_msg("usage: sessions kill <session_id>") + PROMPT]
    env = ctx.get("env")
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


async def cmd_kv(ctx: dict[str, Any], arg: str) -> list[str]:
    """Dispatch ``kv list|get|set|delete`` subcommands."""
    env = ctx.get("env")
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
