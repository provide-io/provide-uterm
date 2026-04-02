#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.shell.terminal._connector — UshellConnector."""

from unittest.mock import AsyncMock, patch

from provide.terminal.shell._output import BANNER, PROMPT
from provide.terminal.shell.terminal._connector import UshellConnector

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
# extra_ctx propagation (mutation killers)
# ---------------------------------------------------------------------------


async def test_extra_ctx_available_via_py_command():
    # Kills mutmut_18: CommandDispatcher(ctx, None) creates a sandbox
    # without extra_ctx names, so `py MY_CTX` would NameError.
    conn = UshellConnector("s1", extra_ctx={"MY_CTX": 99})
    await conn.start()
    frames = await conn.handle_input("py MY_CTX\r")
    data = " ".join(f["data"] for f in frames)
    assert "99" in data


async def test_extra_ctx_visible_in_env_command():
    # Kills mutmut_13/17: ctx=None means self._ctx is None; env calls
    # self._ctx.get(...) which raises AttributeError on None.
    conn = UshellConnector("s1", extra_ctx={"MY_KEY": "hello"})
    await conn.start()
    frames = await conn.handle_input("env\r")
    data = " ".join(f["data"] for f in frames)
    assert "MY_KEY" in data


# ---------------------------------------------------------------------------
# stop — precise boolean (kills stop__mutmut_1: False → None)
# ---------------------------------------------------------------------------


async def test_stop_connected_is_exactly_false():
    conn = UshellConnector("s1")
    await conn.start()
    await conn.stop()
    assert conn.is_connected() is False


# ---------------------------------------------------------------------------
# get_analysis — structure and filter (kills get_analysis__mutmut_2,4,6)
# ---------------------------------------------------------------------------


async def test_get_analysis_line_structure():
    # Kills mutmut_2: "\n".join → "XX\nXX".join changes line prefixes
    conn = UshellConnector("s1")
    await conn.start()
    analysis = await conn.get_analysis()
    lines = analysis.split("\n")
    assert lines[0].startswith("[ushell analysis")
    assert lines[1].startswith("connected:")
    assert lines[2].startswith("current_line:")
    assert lines[3].startswith("sandbox_names:")


async def test_get_analysis_sandbox_names_excludes_dunders():
    # Kills mutmut_4 (filter inverted) and mutmut_6 (prefix 'XX__XX')
    conn = UshellConnector("s1")
    analysis = await conn.get_analysis()
    sandbox_line = next(ln for ln in analysis.split("\n") if ln.startswith("sandbox_names:"))
    assert "__builtins__" not in sandbox_line


# ---------------------------------------------------------------------------
# get_snapshot — cursor y, screen_hash, prompt_detected
# ---------------------------------------------------------------------------


async def test_get_snapshot_cursor_y_is_one():
    # Kills mutmut_14 ("XXyXX"), mutmut_15 ("Y"), mutmut_16 (y=2)
    conn = UshellConnector("s1")
    await conn.start()
    snap = await conn.get_snapshot()
    assert snap["cursor"]["y"] == 1


async def test_get_snapshot_screen_hash_matches_content():
    # Kills mutmut_25 (hash(None)→str(None)), mutmut_26 (hash(None))
    conn1 = UshellConnector("aaa-session")
    conn2 = UshellConnector("bbb-session")
    await conn1.start()
    await conn2.start()
    snap1 = await conn1.get_snapshot()
    snap2 = await conn2.get_snapshot()
    assert snap1["screen_hash"] != snap2["screen_hash"]


async def test_get_snapshot_screen_hash_derived_from_screen():
    # Kills mutmut_25,26,27 by verifying hash is computed from screen
    conn = UshellConnector("s1")
    await conn.start()
    snap = await conn.get_snapshot()
    screen = snap["screen"]
    assert snap["screen_hash"] == str(hash(screen))[:16]


async def test_get_snapshot_prompt_detected_structure():
    # Kills mutmut_36 ("XXprompt_idXX"), mutmut_37 ("PROMPT_ID"),
    # mutmut_38 ("XXushell_promptXX"), mutmut_39 ("USHELL_PROMPT")
    conn = UshellConnector("s1")
    await conn.start()
    snap = await conn.get_snapshot()
    assert snap["prompt_detected"]["prompt_id"] == "ushell_prompt"


# ---------------------------------------------------------------------------
# poll_messages — sleep duration (kills poll_messages__mutmut_11: 0.05 → 1.05)
# ---------------------------------------------------------------------------


async def test_poll_messages_sleep_duration():
    mock_sleep = AsyncMock()
    with patch("asyncio.sleep", mock_sleep):
        conn = UshellConnector("s1")
        await conn.start()
        await conn.poll_messages()  # first call: welcome frames, no sleep
        await conn.poll_messages()  # second call: should sleep(0.05)
    mock_sleep.assert_awaited_once_with(0.05)


# ---------------------------------------------------------------------------
# __init__ defaults — kills __init____mutmut_1 (session_id "XXXX"),
#                          __init____mutmut_2 (display_name "XXXX"),
#                          __init____mutmut_8 (_welcomed None)
# ---------------------------------------------------------------------------


def test_default_session_id_is_empty_string():
    conn = UshellConnector()
    assert conn._session_id == ""


def test_default_display_name_is_empty_string():
    conn = UshellConnector()
    assert conn._display_name == ""


def test_welcomed_starts_as_exactly_false():
    conn = UshellConnector("s1")
    assert conn._welcomed is False
