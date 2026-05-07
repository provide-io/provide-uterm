#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.shell._commands — CommandDispatcher KV commands."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from provide.terminal.shell.commands import CommandDispatcher


def make_dispatcher(ctx: dict[str, Any] | None = None) -> CommandDispatcher:
    return CommandDispatcher(ctx or {})


def first_data(frames: list[str]) -> str:
    return frames[0]


def make_kv_ctx(kv: Any) -> dict:
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    return {"env": env}


# ---------------------------------------------------------------------------
# kv command
# ---------------------------------------------------------------------------


async def test_cmd_kv_no_env():
    d = make_dispatcher()
    frames = await d.dispatch("kv list")
    assert "not available" in first_data(frames)


async def test_cmd_kv_env_without_session_registry():
    env = SimpleNamespace()  # no SESSION_REGISTRY attr
    d = make_dispatcher({"env": env})
    frames = await d.dispatch("kv list")
    assert "not available" in first_data(frames)


async def test_cmd_kv_list_with_keys_as_dicts():
    kv = AsyncMock()
    result = MagicMock()
    result.keys = [{"name": "session:abc"}, {"name": "session:def"}]
    kv.list = AsyncMock(return_value=result)
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv list")
    data = first_data(frames)
    assert "session:abc" in data


async def test_cmd_kv_list_with_keys_as_objects():
    kv = AsyncMock()
    result = MagicMock()
    key_obj = SimpleNamespace(name="session:xyz")
    result.keys = [key_obj]
    kv.list = AsyncMock(return_value=result)
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv list")
    assert "session:xyz" in first_data(frames)


async def test_cmd_kv_list_keys_as_plain_strings():
    kv = AsyncMock()
    result = MagicMock()
    # Object without .name — falls back to str(k)
    result.keys = ["session:plain"]
    # hasattr(result, 'keys') is True (it's an attribute)
    kv.list = AsyncMock(return_value=result)
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv list")
    assert "session:plain" in first_data(frames)


async def test_cmd_kv_list_empty():
    kv = AsyncMock()
    result = MagicMock()
    result.keys = []
    kv.list = AsyncMock(return_value=result)
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv list")
    assert "no keys" in first_data(frames)


async def test_cmd_kv_list_exception():
    kv = AsyncMock()
    kv.list = AsyncMock(side_effect=Exception("list error"))
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv list")
    assert "list error" in first_data(frames)


async def test_cmd_kv_list_result_as_dict():
    # result without .keys attr — falls back to result.get("keys", [])
    # Use a MagicMock that returns False for hasattr(..., "keys")
    kv = AsyncMock()
    result = MagicMock(spec=[])  # spec=[] means no attributes, so hasattr(result, "keys") is False
    result.get = MagicMock(return_value=[{"name": "session:dictkey"}])
    kv.list = AsyncMock(return_value=result)
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv list")
    assert "session:dictkey" in first_data(frames)


async def test_cmd_kv_get_no_key():
    kv = AsyncMock()
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv get")
    assert "usage: kv get" in first_data(frames)


async def test_cmd_kv_get_with_prefix():
    kv = AsyncMock()
    kv.get = AsyncMock(return_value="some_value")
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv get session:mykey")
    data = first_data(frames)
    assert "some_value" in data
    assert "session:mykey" in data


async def test_cmd_kv_get_without_prefix():
    kv = AsyncMock()
    kv.get = AsyncMock(return_value="val")
    d = make_dispatcher(make_kv_ctx(kv))
    await d.dispatch("kv get mykey")
    # should prepend session:
    kv.get.assert_called_once_with("session:mykey")


async def test_cmd_kv_get_missing_key():
    kv = AsyncMock()
    kv.get = AsyncMock(return_value=None)
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv get missing")
    assert "key not found" in first_data(frames)


async def test_cmd_kv_get_exception():
    kv = AsyncMock()
    kv.get = AsyncMock(side_effect=Exception("get error"))
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv get mykey")
    assert "get error" in first_data(frames)


async def test_cmd_kv_invalid_subcommand():
    kv = AsyncMock()
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv foo")
    assert "usage: kv list" in first_data(frames)


async def test_cmd_kv_no_subcommand():
    kv = AsyncMock()
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv")
    assert "usage: kv list" in first_data(frames)


# ---------------------------------------------------------------------------
# kv set tests
# ---------------------------------------------------------------------------


async def test_cmd_kv_set_no_args():
    kv = AsyncMock()
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv set")
    assert "usage: kv set" in first_data(frames)


async def test_cmd_kv_set_missing_value():
    kv = AsyncMock()
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv set mykey")
    assert "usage: kv set" in first_data(frames)


async def test_cmd_kv_set_success():
    kv = AsyncMock()
    kv.put = AsyncMock()
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv set mykey value")
    kv.put.assert_called_once_with("session:mykey", "value")
    assert "set" in first_data(frames)


async def test_cmd_kv_set_with_prefix():
    kv = AsyncMock()
    kv.put = AsyncMock()
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv set session:mykey value")
    kv.put.assert_called_once_with("session:mykey", "value")
    assert "set" in first_data(frames)


async def test_cmd_kv_set_exception():
    kv = AsyncMock()
    kv.put = AsyncMock(side_effect=Exception("put error"))
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv set mykey value")
    assert "put error" in first_data(frames)


# ---------------------------------------------------------------------------
# kv delete tests
# ---------------------------------------------------------------------------


async def test_cmd_kv_delete_no_arg():
    kv = AsyncMock()
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv delete")
    assert "usage: kv delete" in first_data(frames)


async def test_cmd_kv_delete_success():
    kv = AsyncMock()
    kv.delete = AsyncMock()
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv delete mykey")
    kv.delete.assert_called_once_with("session:mykey")
    assert "deleted" in first_data(frames)


async def test_cmd_kv_delete_with_prefix():
    kv = AsyncMock()
    kv.delete = AsyncMock()
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv delete session:mykey")
    kv.delete.assert_called_once_with("session:mykey")
    assert "deleted" in first_data(frames)


async def test_cmd_kv_delete_exception():
    kv = AsyncMock()
    kv.delete = AsyncMock(side_effect=Exception("delete error"))
    d = make_dispatcher(make_kv_ctx(kv))
    frames = await d.dispatch("kv delete mykey")
    assert "delete error" in first_data(frames)
