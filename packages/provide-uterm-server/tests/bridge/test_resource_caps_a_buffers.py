#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for Sub-fix A: per-connection input and hold buffer exhaustion caps.

Covers:
- hub.state.buffer_and_get_command drops the buffer and returns None when the
  cumulative per-ws input exceeds hub.max_buffer_chars.
- _handle_input (paused browser path) sends an error frame and does NOT append
  data when the hold buffer would exceed hub.max_buffer_chars.
- Normal short input through both paths still works.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState
from provide.uterm.server.bridge.routes.browser_handlers import handle_browser_message

# ---------------------------------------------------------------------------
# buffer_and_get_command cap (store.py)
# ---------------------------------------------------------------------------


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


async def test_buffer_cap_drops_overflow_and_returns_none() -> None:
    """buffer_and_get_command must discard buffer + return None when cap exceeded."""
    # max_input_chars=200 so max_buffer_chars=300 is used as-is (300 > 200)
    hub = TermHub(max_input_chars=200, max_buffer_chars=300)
    ws = _make_ws()

    # Feed data up to 1 short of cap — should still be buffered
    chunk = "x" * 290
    result = hub._buffer_and_get_command(ws, chunk)
    assert result is None
    assert ws in hub._input_buffers

    # Now push past the cap — buffer must be cleared, None returned
    overflow = "y" * 20  # total would be 310 > 300
    result = hub._buffer_and_get_command(ws, overflow)
    assert result is None, "overflowing buffer must return None"
    assert ws not in hub._input_buffers, "overflowing buffer must be cleared"


async def test_buffer_cap_not_triggered_at_exact_cap() -> None:
    """buffer_and_get_command must NOT drop when len == cap (only when strictly over)."""
    # max_input_chars=200, max_buffer_chars=300 → effective cap = 300
    hub = TermHub(max_input_chars=200, max_buffer_chars=300)
    ws = _make_ws()

    # Exactly at the cap — buffer is written, no drop
    chunk = "x" * 300
    result = hub._buffer_and_get_command(ws, chunk)
    assert result is None
    assert ws in hub._input_buffers


async def test_buffer_cap_short_input_works() -> None:
    """Normal short input must accumulate and return command on newline."""
    hub = TermHub(max_input_chars=200, max_buffer_chars=300)
    ws = _make_ws()

    hub._buffer_and_get_command(ws, "echo ")
    result = hub._buffer_and_get_command(ws, "hi\r")
    assert result == "echo hi\r"
    assert ws not in hub._input_buffers


async def test_buffer_cap_respects_max_buffer_chars_attr() -> None:
    """hub.max_buffer_chars must be at least max_input_chars (floor)."""
    hub = TermHub(max_input_chars=10_000, max_buffer_chars=5_000)
    # max_buffer_chars is clamped to max(max_input_chars, max_buffer_chars)
    assert hub.max_buffer_chars >= hub.max_input_chars


# ---------------------------------------------------------------------------
# _handle_input paused-browser hold buffer cap (browser_handlers.py)
# ---------------------------------------------------------------------------


async def _register(hub: TermHub, worker_id: str, browser_ws: MagicMock, role: str) -> None:
    async with hub._lock:
        st = hub._workers.setdefault(worker_id, WorkerTermState())
        st.browsers[browser_ws] = role


async def test_hold_buffer_cap_sends_error_and_drops_data() -> None:
    """Paused browser sending data past max_buffer_chars must get error frame + drop."""
    # max_input_chars=200, max_buffer_chars=300 → effective cap = 300
    hub = TermHub(max_input_chars=200, max_buffer_chars=300)
    ws = _make_ws()
    await _register(hub, "w1", ws, "operator")

    # Mark browser as paused
    hub._paused_browsers.add(ws)

    # Pre-fill hold buffer near the cap
    hub._hold_buffers[ws] = "x" * 290

    # Send a chunk that would push past the cap
    overflow_data = "y" * 20  # 290 + 20 = 310 > 300
    await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": overflow_data}, False)

    # Must have sent an error frame
    assert ws.send_text.called, "error frame must be sent on hold buffer overflow"
    error_text = ws.send_text.call_args[0][0]
    assert len(error_text) > 0

    # Hold buffer must NOT have grown (overflow data not appended)
    assert len(hub._hold_buffers.get(ws, "")) <= 300, "hold buffer must not exceed cap"
    assert overflow_data not in hub._hold_buffers.get(ws, ""), "overflow data must not be appended"


async def test_hold_buffer_cap_normal_data_buffered() -> None:
    """Paused browser sending data within cap must be buffered normally."""
    # max_input_chars=200, max_buffer_chars=300 → effective cap = 300
    hub = TermHub(max_input_chars=200, max_buffer_chars=300)
    ws = _make_ws()
    await _register(hub, "w1", ws, "operator")

    hub._paused_browsers.add(ws)

    small_data = "ls -la\r"
    await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": small_data}, False)

    # No error, data buffered
    ws.send_text.assert_not_called()
    assert hub._hold_buffers.get(ws, "") == small_data


async def test_hold_buffer_cap_empty_initial_buffer_within_cap() -> None:
    """Hold buffer overflow check is correct when the existing buffer is empty."""
    # max_input_chars=200, max_buffer_chars=400 → effective cap = 400
    hub = TermHub(max_input_chars=200, max_buffer_chars=400)
    ws = _make_ws()
    await _register(hub, "w1", ws, "operator")

    hub._paused_browsers.add(ws)

    # Data that fits
    fitting_data = "x" * 350
    await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": fitting_data}, False)
    assert hub._hold_buffers.get(ws) == fitting_data
    ws.send_text.assert_not_called()

    # Now overflow
    overflow_data = "y" * 60  # 350 + 60 = 410 > 400
    await handle_browser_message(hub, ws, "w1", "operator", {"type": "input", "data": overflow_data}, False)
    assert ws.send_text.called
    # Hold buffer must still be exactly fitting_data (overflow not appended)
    assert hub._hold_buffers.get(ws) == fitting_data
