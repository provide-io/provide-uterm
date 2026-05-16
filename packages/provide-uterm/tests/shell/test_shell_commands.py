#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.shell._commands — CommandDispatcher."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from provide.uterm.shell.commands import CommandDispatcher


def make_dispatcher(ctx: dict[str, Any] | None = None) -> CommandDispatcher:
    return CommandDispatcher(ctx or {})


def first_data(frames: list[str]) -> str:
    return frames[0]


# ---------------------------------------------------------------------------
# dispatch — basic routing
# ---------------------------------------------------------------------------


async def test_dispatch_empty_line():
    d = make_dispatcher()
    frames = await d.dispatch("")
    assert frames[0].endswith(" ")  # PROMPT ends with space


async def test_dispatch_ctrl_c():
    d = make_dispatcher()
    frames = await d.dispatch("\x03")
    assert len(frames) == 1


async def test_dispatch_exit():
    d = make_dispatcher()
    frames = await d.dispatch("exit")
    assert "Goodbye" in first_data(frames)


async def test_dispatch_quit():
    d = make_dispatcher()
    frames = await d.dispatch("quit")
    assert "Goodbye" in first_data(frames)


async def test_dispatch_ctrl_d():
    d = make_dispatcher()
    frames = await d.dispatch("\x04")
    assert "Goodbye" in first_data(frames)


async def test_dispatch_help():
    d = make_dispatcher()
    frames = await d.dispatch("help")
    assert "ushell commands" in first_data(frames)


async def test_dispatch_clear():
    d = make_dispatcher()
    frames = await d.dispatch("clear")
    # clear screen escape
    assert "\x1b[2J" in first_data(frames)


async def test_dispatch_unknown_command():
    d = make_dispatcher()
    frames = await d.dispatch("bogus")
    assert "unknown command" in first_data(frames)


async def test_dispatch_case_insensitive():
    d = make_dispatcher()
    frames = await d.dispatch("HELP")
    assert "ushell commands" in first_data(frames)


async def test_dispatch_strips_whitespace():
    d = make_dispatcher()
    frames = await d.dispatch("  help  ")
    assert "ushell commands" in first_data(frames)


# ---------------------------------------------------------------------------
# py command
# ---------------------------------------------------------------------------


async def test_cmd_py_no_arg():
    d = make_dispatcher()
    frames = await d.dispatch("py")
    assert "usage: py" in first_data(frames)


async def test_cmd_py_expression():
    d = make_dispatcher()
    frames = await d.dispatch("py 2+2")
    assert "4" in first_data(frames)


async def test_cmd_py_no_output_shows_ok():
    d = make_dispatcher()
    frames = await d.dispatch("py None")
    assert "ok" in first_data(frames)


async def test_cmd_py_statement():
    d = make_dispatcher()
    frames = await d.dispatch("py x = 5")
    # assignment → no result, shows "ok"
    assert "ok" in first_data(frames)


# ---------------------------------------------------------------------------
# sessions command
# ---------------------------------------------------------------------------


async def test_cmd_sessions_no_list_fn():
    d = make_dispatcher()
    frames = await d.dispatch("sessions")
    assert "not available" in first_data(frames)


async def test_cmd_sessions_empty():
    async def list_fn():
        return []

    d = make_dispatcher({"list_kv_sessions": list_fn})
    frames = await d.dispatch("sessions")
    assert "no sessions" in first_data(frames)


async def test_cmd_sessions_with_data():
    async def list_fn():
        return [
            {"session_id": "s1", "lifecycle_state": "running", "connector_type": "shell", "connected": True},
            {"session_id": "s2", "lifecycle_state": "idle", "connector_type": "telnet", "connected": False},
        ]

    d = make_dispatcher({"list_kv_sessions": list_fn})
    frames = await d.dispatch("sessions")
    data = first_data(frames)
    assert "s1" in data
    assert "live" in data
    assert "idle" in data


async def test_cmd_sessions_exception():
    async def list_fn():
        raise RuntimeError("kv error")

    d = make_dispatcher({"list_kv_sessions": list_fn})
    frames = await d.dispatch("sessions")
    assert "kv error" in first_data(frames)


async def test_cmd_sessions_missing_fields():
    async def list_fn():
        return [{}]  # all fields missing → defaults to "?"

    d = make_dispatcher({"list_kv_sessions": list_fn})
    frames = await d.dispatch("sessions")
    assert "?" in first_data(frames)


# ---------------------------------------------------------------------------
# env command
# ---------------------------------------------------------------------------


async def test_cmd_env_no_env():
    d = make_dispatcher({"my_key": "val"})
    frames = await d.dispatch("env")
    assert "my_key" in first_data(frames)


async def test_cmd_env_with_env_object():
    env = SimpleNamespace(SESSION_REGISTRY=object(), _private="hidden")
    d = make_dispatcher({"env": env})
    frames = await d.dispatch("env")
    data = first_data(frames)
    assert "SESSION_REGISTRY" in data
    assert "_private" not in data


async def test_cmd_env_empty_ctx():
    d = make_dispatcher({})
    frames = await d.dispatch("env")
    assert "(empty context)" in first_data(frames)


async def test_cmd_env_with_env_no_public_attrs():
    # env object with only private attrs → shows context heading with no lines → empty context? No:
    # lines would be empty → output = info_msg("(empty context)")
    class _EmptyEnv:
        _x = 1

    d = make_dispatcher({"env": _EmptyEnv()})
    frames = await d.dispatch("env")
    # lines is empty → "(empty context)"
    assert "(empty context)" in first_data(frames)


# ---------------------------------------------------------------------------
# help <cmd> tests
# ---------------------------------------------------------------------------


async def test_help_cmd_kv():
    d = make_dispatcher()
    frames = await d.dispatch("help kv")
    assert "kv" in first_data(frames)
    assert "usage" not in first_data(frames).lower() or "kv list" in first_data(frames)


async def test_help_cmd_py():
    d = make_dispatcher()
    frames = await d.dispatch("help py")
    data = first_data(frames)
    assert "py" in data


async def test_help_cmd_bogus():
    d = make_dispatcher()
    frames = await d.dispatch("help bogus")
    assert "no help for" in first_data(frames)


# ---------------------------------------------------------------------------
# sessions kill tests
# ---------------------------------------------------------------------------


async def test_cmd_sessions_kill_no_id():
    d = make_dispatcher()
    frames = await d.dispatch("sessions kill")
    assert "usage: sessions kill" in first_data(frames)


async def test_cmd_sessions_kill_no_binding():
    d = make_dispatcher()
    frames = await d.dispatch("sessions kill sid1")
    assert "not available" in first_data(frames)


async def test_cmd_sessions_kill_success():
    fake_stub = AsyncMock()
    fake_stub.fetch = AsyncMock()
    fake_ns = MagicMock()
    fake_ns.idFromName = MagicMock(return_value="stub_id")
    fake_ns.get = MagicMock(return_value=fake_stub)
    env = SimpleNamespace(SESSION_RUNTIME=fake_ns)
    d = make_dispatcher({"env": env})
    frames = await d.dispatch("sessions kill sid1")
    assert "kill signal sent" in first_data(frames)


async def test_cmd_sessions_kill_exception():
    fake_ns = MagicMock()
    fake_ns.idFromName = MagicMock(side_effect=Exception("do error"))
    env = SimpleNamespace(SESSION_RUNTIME=fake_ns)
    d = make_dispatcher({"env": env})
    frames = await d.dispatch("sessions kill sid1")
    assert "do error" in first_data(frames)
