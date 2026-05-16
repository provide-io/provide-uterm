#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.shell._commands — cast command."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

from provide.uterm.shell.commands import AnimatedResult, CommandDispatcher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_dispatcher(ctx: dict[str, Any] | None = None) -> CommandDispatcher:
    return CommandDispatcher(ctx or {})


def first_data(frames: list[str]) -> str:
    return frames[0]


def _make_cast_text(events: list[tuple[float, str]], version: int = 2) -> str:
    """Build a minimal asciicast v2 text string."""
    header = json.dumps({"version": version, "width": 80, "height": 24})
    lines = [header]
    for ts, data in events:
        lines.append(json.dumps([ts, "o", data]))
    return "\n".join(lines)


def _mock_urlopen(data: bytes, status: int = 200) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_resp.status = status
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_cast_no_args():
    d = make_dispatcher()
    result = await d.dispatch("cast")
    assert isinstance(result, list)
    assert "usage:" in first_data(result)


async def test_cast_in_help():
    d = make_dispatcher()
    result = await d.dispatch("help")
    assert isinstance(result, list)
    assert "cast" in first_data(result)


async def test_cast_help_detail():
    d = make_dispatcher()
    result = await d.dispatch("help cast")
    assert isinstance(result, list)
    assert "--fps" in first_data(result)


async def test_cast_unknown_flag():
    d = make_dispatcher()
    result = await d.dispatch("cast --bogus https://example.com/demo.cast")
    assert isinstance(result, list)
    assert "unknown flag" in first_data(result)


async def test_cast_bad_url_scheme():
    d = make_dispatcher()
    result = await d.dispatch("cast ftp://example.com/demo.cast")
    assert isinstance(result, list)
    assert "unsupported URL scheme" in first_data(result)


async def test_cast_file_not_found():
    d = make_dispatcher()
    result = await d.dispatch("cast file:///nonexistent/path/demo.cast")
    assert isinstance(result, list)
    assert "file not found" in first_data(result)


async def test_cast_file_url_success(tmp_path):
    cast_text = _make_cast_text([(0.0, "hello "), (0.5, "world\r\n")])
    cast_file = tmp_path / "demo.cast"
    cast_file.write_text(cast_text, encoding="utf-8")
    d = make_dispatcher()
    result = await d.dispatch(f"cast file://{cast_file}")
    assert isinstance(result, AnimatedResult)
    assert len(result.frames) > 1
    assert result.loop is False


async def test_cast_http_success():
    cast_text = _make_cast_text([(0.0, "hi\r\n"), (1.0, "bye\r\n")])
    d = make_dispatcher()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(cast_text.encode())):
        result = await d.dispatch("cast https://example.com/demo.cast")
    assert isinstance(result, AnimatedResult)
    assert len(result.frames) > 1


async def test_cast_network_error():
    d = make_dispatcher()
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        result = await d.dispatch("cast https://example.com/demo.cast")
    assert isinstance(result, list)
    assert "cannot fetch" in first_data(result)


async def test_cast_empty_file():
    d = make_dispatcher()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(b"")):
        result = await d.dispatch("cast https://example.com/demo.cast")
    assert isinstance(result, list)
    assert "empty cast file" in first_data(result)


async def test_cast_invalid_header_json():
    d = make_dispatcher()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(b"NOT JSON\n[0.0,'o','hi']")):
        result = await d.dispatch("cast https://example.com/demo.cast")
    assert isinstance(result, list)
    assert "invalid cast header" in first_data(result)


async def test_cast_wrong_version():
    cast_text = json.dumps({"version": 1, "width": 80, "height": 24}) + "\n"
    d = make_dispatcher()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(cast_text.encode())):
        result = await d.dispatch("cast https://example.com/demo.cast")
    assert isinstance(result, list)
    assert "unsupported asciicast version" in first_data(result)


async def test_cast_no_output_events():
    # Only header, no events
    cast_text = json.dumps({"version": 2, "width": 80, "height": 24}) + "\n"
    # Add a non-"o" event (stdin)
    cast_text += json.dumps([0.5, "i", "keystroke"]) + "\n"
    d = make_dispatcher()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(cast_text.encode())):
        result = await d.dispatch("cast https://example.com/demo.cast")
    assert isinstance(result, list)
    assert "no output events" in first_data(result)


async def test_cast_only_empty_buckets():
    """All events at t=0 → frame list stays at just the clear-screen → no displayable output."""
    cast_text = _make_cast_text([])
    # Add only events that are malformed so they get skipped
    header = json.dumps({"version": 2, "width": 80, "height": 24})
    cast_text = header + "\n" + "INVALID_JSON\n"
    d = make_dispatcher()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(cast_text.encode())):
        result = await d.dispatch("cast https://example.com/demo.cast")
    assert isinstance(result, list)
    assert "no output events" in first_data(result)


async def test_cast_loop_flag(tmp_path):
    cast_text = _make_cast_text([(0.0, "hello\r\n"), (0.5, "world\r\n")])
    cast_file = tmp_path / "demo.cast"
    cast_file.write_text(cast_text, encoding="utf-8")
    d = make_dispatcher()
    result = await d.dispatch(f"cast --loop file://{cast_file}")
    assert isinstance(result, AnimatedResult)
    assert result.loop is True


async def test_cast_fps_flag(tmp_path):
    cast_text = _make_cast_text([(0.0, "hello\r\n"), (2.0, "world\r\n")])
    cast_file = tmp_path / "demo.cast"
    cast_file.write_text(cast_text, encoding="utf-8")
    d = make_dispatcher()
    result = await d.dispatch(f"cast --fps 5 file://{cast_file}")
    assert isinstance(result, AnimatedResult)
    assert result.fps == 5.0


async def test_cast_skips_malformed_event_lines(tmp_path):
    """Lines that aren't valid JSON or aren't lists are silently skipped."""
    header = json.dumps({"version": 2, "width": 80, "height": 24})
    lines = [
        header,
        "BROKEN",
        json.dumps([0.0, "o", "good output\r\n"]),
        '{"not": "a list"}',
        json.dumps([1.0, "o", "more output\r\n"]),
    ]
    cast_text = "\n".join(lines)
    cast_file = tmp_path / "demo.cast"
    cast_file.write_text(cast_text, encoding="utf-8")
    d = make_dispatcher()
    result = await d.dispatch(f"cast file://{cast_file}")
    assert isinstance(result, AnimatedResult)
    assert any("good output" in f for f in result.frames)


async def test_cast_empty_leading_buckets(tmp_path):
    """Events at a non-zero timestamp produce empty leading frame buckets (line 582→581 branch)."""
    # First event at t=2.0 means ~30 empty leading buckets at 15fps
    cast_text = _make_cast_text([(2.0, "delayed output\r\n")])
    cast_file = tmp_path / "demo.cast"
    cast_file.write_text(cast_text, encoding="utf-8")
    d = make_dispatcher()
    result = await d.dispatch(f"cast file://{cast_file}")
    assert isinstance(result, AnimatedResult)
    assert any("delayed output" in f for f in result.frames)


async def test_cast_all_empty_data_no_displayable(tmp_path):
    """Events with empty string data produce no displayable frames (line 587 branch)."""
    cast_text = _make_cast_text([(0.0, ""), (0.1, "")])
    cast_file = tmp_path / "demo.cast"
    cast_file.write_text(cast_text, encoding="utf-8")
    d = make_dispatcher()
    result = await d.dispatch(f"cast file://{cast_file}")
    assert isinstance(result, list)
    assert "no displayable output" in first_data(result)
