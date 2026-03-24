#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.shell.terminal._connector — UshellConnector."""

import importlib
import sys

from provide.shell._output import BANNER, PROMPT
from provide.shell.terminal._connector import UshellConnector

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_start_sets_connected():
    conn = UshellConnector("test-session")
    assert not conn.is_connected()
    await conn.start()
    assert conn.is_connected()


async def test_stop_clears_connected():
    conn = UshellConnector("test-session")
    await conn.start()
    await conn.stop()
    assert not conn.is_connected()


async def test_default_session_id():
    conn = UshellConnector()
    assert conn._session_id == ""
    assert conn._display_name == ""


async def test_display_name_defaults_to_session_id():
    conn = UshellConnector("my-id")
    assert conn._display_name == "my-id"


async def test_display_name_override():
    conn = UshellConnector("my-id", display_name="Pretty Name")
    assert conn._display_name == "Pretty Name"


async def test_extra_ctx_injected():
    conn = UshellConnector("s1", extra_ctx={"MY_CTX": 42})
    # extra_ctx available in sandbox namespace
    out = conn._sandbox.run("MY_CTX")
    assert "42" in out


# ---------------------------------------------------------------------------
# poll_messages
# ---------------------------------------------------------------------------


async def test_poll_messages_not_connected():
    conn = UshellConnector("s1")
    frames = await conn.poll_messages()
    assert frames == []


async def test_poll_messages_connected():
    conn = UshellConnector("s1")
    await conn.start()
    frames = await conn.poll_messages()
    assert len(frames) == 2
    # first: worker_hello
    assert frames[0]["type"] == "worker_hello"
    assert frames[0]["input_mode"] == "open"
    # second: banner + prompt
    assert frames[1]["type"] == "term"
    assert BANNER in frames[1]["data"]
    assert PROMPT in frames[1]["data"]


# ---------------------------------------------------------------------------
# handle_input
# ---------------------------------------------------------------------------


async def test_handle_input_printable_echo():
    conn = UshellConnector("s1")
    await conn.start()
    frames = await conn.handle_input("abc")
    assert len(frames) == 1
    assert frames[0]["data"] == "abc"


async def test_handle_input_enter_dispatches():
    conn = UshellConnector("s1")
    await conn.start()
    frames = await conn.handle_input("help\r")
    # echo frame + command output frame(s)
    assert len(frames) >= 2
    data_all = " ".join(f["data"] for f in frames)
    assert "ushell commands" in data_all


async def test_handle_input_no_echo_no_completed():
    # control byte that produces no echo and no completed line
    conn = UshellConnector("s1")
    await conn.start()
    frames = await conn.handle_input("\x01")  # Ctrl+A — ignored
    assert frames == []


async def test_handle_input_ctrl_c():
    conn = UshellConnector("s1")
    await conn.start()
    frames = await conn.handle_input("\x03")
    # echo (^C\r\n) + dispatch (prompt only)
    data_all = " ".join(f["data"] for f in frames)
    assert "^C" in data_all


# ---------------------------------------------------------------------------
# handle_control
# ---------------------------------------------------------------------------


async def test_handle_control_returns_empty():
    conn = UshellConnector("s1")
    assert await conn.handle_control("pause") == []
    assert await conn.handle_control("resume") == []
    assert await conn.handle_control("step") == []


# ---------------------------------------------------------------------------
# get_snapshot
# ---------------------------------------------------------------------------


async def test_get_snapshot_structure():
    conn = UshellConnector("my-session")
    await conn.start()
    snap = await conn.get_snapshot()
    assert snap["type"] == "snapshot"
    assert "my-session" in snap["screen"]
    assert isinstance(snap["cursor"], dict)
    assert snap["cols"] == 80
    assert snap["rows"] == 24
    assert snap["cursor_at_end"] is True
    assert snap["has_trailing_space"] is False
    assert "ts" in snap
    assert "screen_hash" in snap
    assert "prompt_detected" in snap


async def test_get_snapshot_cursor_updates_with_input():
    conn = UshellConnector("s1")
    await conn.start()
    await conn.handle_input("hello")
    snap = await conn.get_snapshot()
    # cursor x should reflect prompt width + len("hello")
    assert snap["cursor"]["x"] == len(PROMPT) + 5


# ---------------------------------------------------------------------------
# get_analysis
# ---------------------------------------------------------------------------


async def test_get_analysis_structure():
    conn = UshellConnector("s1")
    await conn.start()
    analysis = await conn.get_analysis()
    assert "[ushell analysis" in analysis
    assert "connected: True" in analysis
    assert "current_line:" in analysis
    assert "sandbox_names:" in analysis


async def test_get_analysis_not_connected():
    conn = UshellConnector("s1")
    analysis = await conn.get_analysis()
    assert "connected: False" in analysis


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


async def test_clear_returns_clear_frame():
    conn = UshellConnector("s1")
    await conn.start()
    await conn.handle_input("some text")
    frames = await conn.clear()
    assert len(frames) == 1
    assert "\x1b[2J" in frames[0]["data"]
    # line buffer should be cleared
    assert conn._buf.current_line() == ""


# ---------------------------------------------------------------------------
# set_mode
# ---------------------------------------------------------------------------


async def test_set_mode_returns_worker_hello():
    conn = UshellConnector("s1")
    frames = await conn.set_mode("hijack")
    assert len(frames) == 1
    assert frames[0]["type"] == "worker_hello"
    assert frames[0]["input_mode"] == "hijack"


async def test_set_mode_open():
    conn = UshellConnector("s1")
    frames = await conn.set_mode("open")
    assert frames[0]["input_mode"] == "open"


# ---------------------------------------------------------------------------
# config param (unused but should not crash)
# ---------------------------------------------------------------------------


async def test_config_param_accepted():
    conn = UshellConnector("s1", _config={"unused": True})
    await conn.start()
    assert conn.is_connected()


# ---------------------------------------------------------------------------
# ImportError fallback for SessionConnector base class
# ---------------------------------------------------------------------------


def test_session_connector_import_error_falls_back_to_object():
    """Cover the except-ImportError branch in _connector.py (lines 35-36).

    When provide.terminal is not installed, UshellConnector falls back to
    inheriting from object instead of SessionConnector.
    """
    import provide.shell.terminal._connector as mod_ref

    base_module = "provide.terminal.server.connectors.base"
    _sentinel = object()
    orig = sys.modules.get(base_module, _sentinel)

    # Block the import so the except-branch fires on reimport.
    sys.modules[base_module] = None  # type: ignore[assignment]
    try:
        importlib.reload(mod_ref)
        # After reload with blocked import, _SessionConnector should be object.
        assert mod_ref._SessionConnector is object
        # UshellConnector is still functional.
        conn = mod_ref.UshellConnector("fallback-test")
        assert conn._session_id == "fallback-test"
    finally:
        if orig is _sentinel:
            sys.modules.pop(base_module, None)
        else:
            sys.modules[base_module] = orig  # type: ignore[assignment]
        # Reload to restore the module to its correct state.
        importlib.reload(mod_ref)
