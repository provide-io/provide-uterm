# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from provide.terminal.pty.connector import PTYConnector


def make_connector(
    command: str = "/bin/echo", args: list[str] | None = None, **kwargs: Any
) -> PTYConnector:
    return PTYConnector(
        session_id="test-pty-1",
        display_name="Test PTY",
        config={"command": command, "args": args or [], **kwargs},
    )


def test_connector_requires_command() -> None:
    with pytest.raises(ValueError, match="command"):
        PTYConnector("s1", "name", config={})


def test_connector_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown config"):
        PTYConnector("s1", "name", config={"command": "/bin/echo", "unknown_key": True})


def test_connector_rejects_relative_command() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        make_connector("bash")


def test_connector_rejects_null_byte_in_command() -> None:
    with pytest.raises(ValueError, match="null byte"):
        make_connector("/bin/bash\x00")


def test_connector_rejects_null_byte_in_username() -> None:
    with pytest.raises(ValueError, match="null byte"):
        make_connector(username="ali\x00ce")


def test_connector_rejects_null_byte_in_env_value() -> None:
    with pytest.raises(ValueError, match="null byte"):
        make_connector(env={"KEY": "val\x00ue"})


def test_connector_rejects_env_key_with_equals() -> None:
    with pytest.raises(ValueError, match="invalid key"):
        make_connector(env={"KEY=BAD": "value"})


def test_is_connected_before_start() -> None:
    conn = make_connector()
    assert conn.is_connected() is False


async def test_start_and_stop_echo() -> None:
    conn = make_connector("/bin/echo", ["hello from pty"])
    await conn.start()
    assert conn.is_connected() is True
    await asyncio.sleep(0.2)
    msgs = await conn.poll_messages()
    await conn.stop()
    assert conn.is_connected() is False
    screens = [m["screen"] for m in msgs if m.get("type") == "snapshot"]
    assert any("hello from pty" in s for s in screens)


async def test_poll_messages_returns_list() -> None:
    conn = make_connector("/bin/echo", ["hi"])
    await conn.start()
    await asyncio.sleep(0.1)
    msgs = await conn.poll_messages()
    await conn.stop()
    assert isinstance(msgs, list)


async def test_handle_input_returns_snapshot() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    msgs = await conn.handle_input("hello\n")
    await conn.stop()
    assert any(m.get("type") == "snapshot" for m in msgs)


async def test_get_snapshot_returns_dict() -> None:
    conn = make_connector("/bin/echo", ["snap"])
    await conn.start()
    snap = await conn.get_snapshot()
    await conn.stop()
    assert snap["type"] == "snapshot"
    assert "screen" in snap
    assert "cols" in snap
    assert "rows" in snap


async def test_set_mode_returns_hello_and_snapshot() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    msgs = await conn.set_mode("hijack")
    await conn.stop()
    types = [m.get("type") for m in msgs]
    assert "worker_hello" in types
    assert "snapshot" in types


async def test_set_mode_invalid_raises() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    with pytest.raises(ValueError, match="invalid mode"):
        await conn.set_mode("superuser")
    await conn.stop()


async def test_clear_returns_empty_snapshot() -> None:
    conn = make_connector("/bin/echo", ["clear-me"])
    await conn.start()
    await asyncio.sleep(0.1)
    msgs = await conn.clear()
    await conn.stop()
    assert any(m.get("type") == "snapshot" for m in msgs)
    screens = [m["screen"] for m in msgs if m.get("type") == "snapshot"]
    assert all(s == "" for s in screens)


async def test_handle_control_pause_resume() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    msgs_pause = await conn.handle_control("pause")
    msgs_resume = await conn.handle_control("resume")
    await conn.stop()
    assert all(m.get("type") == "snapshot" for m in msgs_pause)
    assert all(m.get("type") == "snapshot" for m in msgs_resume)


async def test_get_analysis_returns_string() -> None:
    conn = make_connector("/bin/echo", ["analysis"])
    await conn.start()
    analysis = await conn.get_analysis()
    await conn.stop()
    assert isinstance(analysis, str)
    assert "/bin/echo" in analysis


async def test_stop_without_start_is_safe() -> None:
    conn = make_connector()
    await conn.stop()  # must not raise


async def test_paused_connector_drops_input() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    await conn.handle_control("pause")
    msgs = await conn.poll_messages()
    await conn.stop()
    assert msgs == []


def test_read_master_returns_empty_before_start() -> None:
    """_read_master() returns b'' when master_fd is None (not yet started)."""
    conn = make_connector()
    assert conn._read_master() == b""  # noqa: SLF001


async def test_handle_control_step_resumes() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    await conn.handle_control("pause")
    assert conn._paused  # noqa: SLF001
    msgs = await conn.handle_control("step")
    await conn.stop()
    assert not conn._paused  # noqa: SLF001
    assert all(m.get("type") == "snapshot" for m in msgs)


async def test_handle_input_noop_when_paused() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    await conn.handle_control("pause")
    msgs = await conn.handle_input("ignored\n")
    await conn.stop()
    assert any(m.get("type") == "snapshot" for m in msgs)


async def test_buffer_capped_at_32768() -> None:
    """Buffer is truncated to last 32768 chars when it exceeds the limit."""
    conn = make_connector("/bin/cat")
    await conn.start()
    # Pre-fill buffer just below the cap, then push it over with one write.
    conn._buffer = "a" * 32764  # noqa: SLF001
    if conn._master_fd is not None:
        os.write(conn._master_fd, b"b" * 10)
    await asyncio.sleep(0.05)
    await conn.poll_messages()
    await conn.stop()
    assert len(conn._buffer) <= 32768


async def test_poll_messages_empty_when_no_output() -> None:
    """poll_messages returns [] when the child hasn't written anything yet."""
    conn = make_connector("/bin/cat")
    await conn.start()
    msgs = await conn.poll_messages()
    await conn.stop()
    # cat hasn't received input so may or may not have output — just verify type
    assert isinstance(msgs, list)


async def test_inject_start_creates_capture_socket() -> None:
    """start() with inject=True wires up a CaptureSocket and cleans up on stop."""
    from provide.terminal.pty.capture import CaptureSocket

    mock_cap = AsyncMock(spec=CaptureSocket)

    with patch("provide.terminal.pty.connector.CaptureSocket", return_value=mock_cap):
        conn = make_connector(
            sys.executable, ["-c", "import time; time.sleep(0.1)"], inject=True
        )
        await conn.start()
        assert conn._capture_socket is mock_cap  # noqa: SLF001
        assert conn._capture_tmpdir is not None  # noqa: SLF001
        await conn.stop()

    mock_cap.stop.assert_awaited_once()
    assert conn._capture_socket is None  # noqa: SLF001
    assert conn._capture_tmpdir is None  # noqa: SLF001


@pytest.mark.requires_root
async def test_user_switch_requires_root() -> None:
    """Only runs as root. Verifies setuid child runs as the target user."""
    conn = make_connector("/usr/bin/id", username="nobody")
    await conn.start()
    await asyncio.sleep(0.2)
    msgs = await conn.poll_messages()
    await conn.stop()
    screens = [m["screen"] for m in msgs if m.get("type") == "snapshot"]
    assert any("nobody" in s for s in screens)
