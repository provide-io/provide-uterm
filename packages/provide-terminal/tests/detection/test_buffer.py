# SPDX-FileCopyrightText: Copyright (c) 2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import time

from provide.terminal.detection.buffer import BufferManager, ScreenBuffer


def test_screen_buffer_creation() -> None:
    sb = ScreenBuffer(screen="hi", screen_hash="h1", snapshot={"screen": "hi"}, captured_at=1.0)
    assert sb.screen == "hi"
    assert sb.matched_prompt_id is None


def test_buffer_manager_add_and_get_recent() -> None:
    mgr = BufferManager(max_size=5)
    for i in range(3):
        mgr.add_screen({"screen": f"s{i}", "screen_hash": f"h{i}", "captured_at": time.time()})
    recent = mgr.get_recent(2)
    assert len(recent) == 2


def test_buffer_manager_max_size_overflow() -> None:
    mgr = BufferManager(max_size=3)
    for i in range(5):
        mgr.add_screen({"screen": f"s{i}", "screen_hash": f"h{i}", "captured_at": time.time()})
    assert len(mgr.get_recent(10)) == 3


def test_buffer_manager_detects_idle() -> None:
    mgr = BufferManager(max_size=5)
    now = time.time()
    mgr.add_screen({"screen": "s", "screen_hash": "same", "captured_at": now - 5})
    mgr.add_screen({"screen": "s", "screen_hash": "same", "captured_at": now})
    assert mgr.detect_idle_state(threshold_seconds=2.0) is True


def test_buffer_manager_not_idle_with_changes() -> None:
    mgr = BufferManager(max_size=5)
    now = time.time()
    mgr.add_screen({"screen": "s1", "screen_hash": "h1", "captured_at": now - 1})
    mgr.add_screen({"screen": "s2", "screen_hash": "h2", "captured_at": now})
    assert mgr.detect_idle_state(threshold_seconds=2.0) is False


def test_buffer_manager_clear() -> None:
    mgr = BufferManager(max_size=5)
    mgr.add_screen({"screen": "s", "screen_hash": "h", "captured_at": time.time()})
    mgr.clear()
    assert mgr.get_recent(5) == []


def test_buffers_backward_compat_property() -> None:
    """_buffers property returns dict with 'default' key wrapping internal deque."""
    mgr = BufferManager(max_size=5)
    mgr.add_screen({"screen": "s", "screen_hash": "h", "captured_at": time.time()})
    buffers = mgr._buffers
    assert "default" in buffers
    assert len(buffers["default"]) == 1


def test_detect_idle_state_before_any_screen() -> None:
    """detect_idle_state returns False on a fresh manager with no screens."""
    mgr = BufferManager(max_size=5)
    # No screens added → _last_change_time is 0.0, _last_hash is ""
    assert mgr.detect_idle_state(threshold_seconds=0.0) is False


def test_get_recent_returns_all_when_n_equals_len() -> None:
    """get_recent(n) where n == buffer length returns all items (n >= len branch)."""
    mgr = BufferManager(max_size=5)
    for i in range(3):
        mgr.add_screen({"screen": f"s{i}", "screen_hash": f"h{i}", "captured_at": float(i)})
    recent = mgr.get_recent(3)  # exactly equal to buffer length
    assert len(recent) == 3
