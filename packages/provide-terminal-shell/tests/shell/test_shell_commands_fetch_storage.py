#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.shell._commands — CommandDispatcher fetch and storage commands."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from provide.terminal.shell._commands import CommandDispatcher


def make_dispatcher(ctx: dict[str, Any] | None = None) -> CommandDispatcher:
    return CommandDispatcher(ctx or {})


def first_data(frames: list[str]) -> str:
    return frames[0]


def make_storage_ctx(storage: Any) -> dict:
    return {"storage": storage}


# ---------------------------------------------------------------------------
# fetch command
# ---------------------------------------------------------------------------


async def test_cmd_fetch_no_url():
    d = make_dispatcher()
    frames = await d.dispatch("fetch")
    assert "usage: fetch" in first_data(frames)


async def test_cmd_fetch_urllib_success():
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = b"Hello world"
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_response):
        d = make_dispatcher()
        frames = await d.dispatch("fetch http://example.com")
    data = first_data(frames)
    assert "HTTP 200" in data
    assert "Hello world" in data


async def test_cmd_fetch_urllib_4xx():
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.read.return_value = b"Not Found"
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_response):
        d = make_dispatcher()
        frames = await d.dispatch("fetch http://example.com/missing")
    data = first_data(frames)
    assert "HTTP 404" in data


async def test_cmd_fetch_urllib_5xx():
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.read.return_value = b"Server Error"
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_response):
        d = make_dispatcher()
        frames = await d.dispatch("fetch http://example.com/error")
    data = first_data(frames)
    assert "HTTP 500" in data


async def test_cmd_fetch_body_truncated():
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = b"X" * 900
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_response):
        d = make_dispatcher()
        frames = await d.dispatch("fetch http://example.com/big")
    data = first_data(frames)
    assert "…" in data


async def test_cmd_fetch_exception():
    with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
        d = make_dispatcher()
        frames = await d.dispatch("fetch http://badhost")
    assert "connection refused" in first_data(frames)


async def test_cmd_fetch_js_fetch_path():
    """Cover the js.fetch branch by injecting a fake 'js' module."""
    import sys
    import types

    fake_resp = MagicMock()
    fake_resp.status = 200

    # Make .text() an async callable
    async def fake_text():
        return "js-body"

    fake_resp.text = fake_text

    async def fake_fetch(url, opts=None):
        return fake_resp

    fake_js = types.ModuleType("js")
    fake_js.fetch = fake_fetch
    sys.modules["js"] = fake_js
    try:
        d = make_dispatcher()
        frames = await d.dispatch("fetch http://example.com")
        data = first_data(frames)
        assert "HTTP 200" in data
        assert "js-body" in data
    finally:
        del sys.modules["js"]


# ---------------------------------------------------------------------------
# fetch -X POST tests
# ---------------------------------------------------------------------------


async def test_cmd_fetch_post_urllib():
    mock_response = MagicMock()
    mock_response.status = 201
    mock_response.read.return_value = b"Created"
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_response):
        d = make_dispatcher()
        frames = await d.dispatch('fetch -X POST http://example.com {"key": "val"}')
    data = first_data(frames)
    assert "HTTP 201" in data
    assert "Created" in data


async def test_cmd_fetch_minus_x_no_method():
    d = make_dispatcher()
    frames = await d.dispatch("fetch -X")
    assert "usage: fetch" in first_data(frames)


async def test_cmd_fetch_minus_x_no_url():
    d = make_dispatcher()
    frames = await d.dispatch("fetch -X POST")
    assert "usage: fetch" in first_data(frames)


async def test_cmd_fetch_js_fetch_post():
    """Cover the js.fetch branch with -X POST."""
    import sys
    import types

    fake_resp = MagicMock()
    fake_resp.status = 200

    async def fake_text():
        return "js-post-body"

    fake_resp.text = fake_text

    async def fake_fetch(url, opts=None):
        return fake_resp

    fake_js = types.ModuleType("js")
    fake_js.fetch = fake_fetch
    sys.modules["js"] = fake_js
    try:
        d = make_dispatcher()
        frames = await d.dispatch("fetch -X POST http://example.com hello")
        data = first_data(frames)
        assert "HTTP 200" in data
        assert "js-post-body" in data
    finally:
        del sys.modules["js"]


# ---------------------------------------------------------------------------
# storage tests
# ---------------------------------------------------------------------------


async def test_cmd_storage_no_storage():
    d = make_dispatcher()
    frames = await d.dispatch("storage list")
    assert "not available" in first_data(frames)


async def test_cmd_storage_list_with_keys():
    storage = AsyncMock()
    result = MagicMock()
    result.keys = [{"name": "key1"}, {"name": "key2"}]
    storage.list = AsyncMock(return_value=result)
    d = make_dispatcher(make_storage_ctx(storage))
    frames = await d.dispatch("storage list")
    data = first_data(frames)
    assert "key1" in data
    assert "key2" in data


async def test_cmd_storage_list_empty():
    storage = AsyncMock()
    result = MagicMock()
    result.keys = []
    storage.list = AsyncMock(return_value=result)
    d = make_dispatcher(make_storage_ctx(storage))
    frames = await d.dispatch("storage list")
    assert "no storage keys found" in first_data(frames)


async def test_cmd_storage_list_exception():
    storage = AsyncMock()
    storage.list = AsyncMock(side_effect=Exception("storage error"))
    d = make_dispatcher(make_storage_ctx(storage))
    frames = await d.dispatch("storage list")
    assert "storage error" in first_data(frames)


async def test_cmd_storage_get_no_key():
    storage = AsyncMock()
    d = make_dispatcher(make_storage_ctx(storage))
    frames = await d.dispatch("storage get")
    assert "usage: storage get" in first_data(frames)


async def test_cmd_storage_get_success():
    storage = AsyncMock()
    storage.get = AsyncMock(return_value="myvalue")
    d = make_dispatcher(make_storage_ctx(storage))
    frames = await d.dispatch("storage get mykey")
    data = first_data(frames)
    assert "myvalue" in data
    assert "mykey" in data


async def test_cmd_storage_get_not_found():
    storage = AsyncMock()
    storage.get = AsyncMock(return_value=None)
    d = make_dispatcher(make_storage_ctx(storage))
    frames = await d.dispatch("storage get mykey")
    assert "key not found" in first_data(frames)


async def test_cmd_storage_get_exception():
    storage = AsyncMock()
    storage.get = AsyncMock(side_effect=Exception("get error"))
    d = make_dispatcher(make_storage_ctx(storage))
    frames = await d.dispatch("storage get mykey")
    assert "get error" in first_data(frames)


async def test_cmd_storage_invalid_subcommand():
    storage = AsyncMock()
    d = make_dispatcher(make_storage_ctx(storage))
    frames = await d.dispatch("storage bogus")
    assert "usage: storage list" in first_data(frames)


async def test_cmd_storage_list_keys_as_objects():
    storage = AsyncMock()
    result = MagicMock()
    key_obj = SimpleNamespace(name="obj_key")
    result.keys = [key_obj]
    storage.list = AsyncMock(return_value=result)
    d = make_dispatcher(make_storage_ctx(storage))
    frames = await d.dispatch("storage list")
    assert "obj_key" in first_data(frames)


async def test_cmd_storage_list_keys_as_plain_strings():
    storage = AsyncMock()
    result = MagicMock()
    result.keys = ["plain_key"]
    storage.list = AsyncMock(return_value=result)
    d = make_dispatcher(make_storage_ctx(storage))
    frames = await d.dispatch("storage list")
    assert "plain_key" in first_data(frames)
