#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.shell.terminal._connector — UshellConnector."""

import asyncio
import contextlib
import importlib
import io
import sys
from unittest.mock import patch

from PIL import Image

from provide.terminal.shell._commands import AnimatedResult
from provide.terminal.shell._output import BANNER, PROMPT
from provide.terminal.shell.terminal._connector import UshellConnector

# ---------------------------------------------------------------------------
# Helper: create a small animated GIF on disk
# ---------------------------------------------------------------------------


def _make_gif_file(tmp_path, n_frames: int = 2) -> str:
    frames = [Image.new("RGB", (4, 4), (i * 100, 255, 0)) for i in range(n_frames)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    p = tmp_path / "anim.gif"
    p.write_bytes(buf.getvalue())
    return str(p)


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


async def test_poll_messages_idle_after_first_call():
    conn = UshellConnector("s1")
    await conn.start()
    await conn.poll_messages()
    assert await conn.poll_messages() == []


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


# ---------------------------------------------------------------------------
# AnimatedResult handling (lines 109-111, 127, 135-145)
# ---------------------------------------------------------------------------


async def test_handle_input_animated_result_starts_stream(tmp_path):
    """handle_input with a render command returning AnimatedResult spawns _stream_animation."""
    gif_path = _make_gif_file(tmp_path, n_frames=2)
    conn = UshellConnector("s1")
    await conn.start()
    # Drain welcome frames
    await conn.poll_messages()

    # Send a render command that returns AnimatedResult (file:// URL, animated GIF)
    frames = await conn.handle_input(f"render file://{gif_path}\r")
    # May have an echo frame — just check it doesn't raise
    assert isinstance(frames, list)

    # Let the background _stream_animation task run at least one iteration
    await asyncio.sleep(0.3)

    # poll_messages should drain _pending_frames (lines 109-111)
    polled = await conn.poll_messages()
    assert isinstance(polled, list)
    # Should have at least some frames from the animation
    assert len(polled) > 0


async def test_handle_input_animated_result_via_mock():
    """Cover handle_input AnimatedResult branch (line 127) and _stream_animation (135-145)."""
    conn = UshellConnector("s1")
    await conn.start()
    await conn.poll_messages()

    animated = AnimatedResult(frames=["frame1\n", "frame2\n"], fps=100.0, loop=False)
    with patch.object(conn._dispatcher, "dispatch", return_value=animated):
        frames = await conn.handle_input("render whatever\r")
    # May have echo frame; AnimatedResult does NOT add to frames directly
    assert isinstance(frames, list)

    # Give _stream_animation time to run both frames + PROMPT
    await asyncio.sleep(0.05)

    # Drain pending frames (lines 109-111)
    polled = await conn.poll_messages()
    data_all = "".join(f["data"] for f in polled)
    assert "frame1" in data_all or "frame2" in data_all or PROMPT in data_all


async def test_stream_animation_loop_then_cancel():
    """Cover _stream_animation with loop=True and CancelledError (line 143)."""
    conn = UshellConnector("s1")
    await conn.start()
    await conn.poll_messages()

    animated = AnimatedResult(frames=["looped\n"], fps=200.0, loop=True)
    task = asyncio.ensure_future(conn._stream_animation(animated))
    # Let it loop a few times
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # After cancellation the task should be done
    assert task.done()


async def test_stop_cancels_running_animation():
    """Cover stop() cancelling a running animation task (line 86)."""
    conn = UshellConnector("s1")
    await conn.start()
    await conn.poll_messages()

    animated = AnimatedResult(frames=["frame\n"], fps=200.0, loop=True)
    conn._animation_task = asyncio.create_task(conn._stream_animation(animated))
    await asyncio.sleep(0.02)
    assert conn._animation_task is not None
    assert not conn._animation_task.done()

    await conn.stop()
    await asyncio.sleep(0.01)
    assert conn._animation_task.done()


async def test_new_render_cancels_previous_animation():
    """Cover handle_input cancelling previous animation before starting new one (line 131)."""
    conn = UshellConnector("s1")
    await conn.start()
    await conn.poll_messages()

    # Start first animation via a looping render
    animated = AnimatedResult(frames=["first\n"], fps=200.0, loop=True)
    conn._animation_task = asyncio.create_task(conn._stream_animation(animated))
    first_task = conn._animation_task
    await asyncio.sleep(0.02)
    assert not first_task.done()

    # Dispatch a second render which should cancel the first
    animated2 = AnimatedResult(frames=["second\n"], fps=200.0, loop=False)
    with patch.object(conn._dispatcher, "dispatch", return_value=animated2):
        conn._buf.feed("render something\r")
        _ = conn._buf.take_echo()
        await conn.handle_input("")  # process completed lines
    # Actually dispatch manually since buf was already fed
    # The handle_input above processed the "render something" line

    await asyncio.sleep(0.05)
    assert first_task.done()  # first was cancelled
    await conn.stop()


def test_session_connector_import_error_falls_back_to_object():
    """Cover the except-ImportError branch in _connector.py (lines 35-36).

    When provide.terminal is not installed, UshellConnector falls back to
    inheriting from object instead of SessionConnector.
    """
    import provide.terminal.shell.terminal._connector as mod_ref

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
