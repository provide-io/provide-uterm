#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import pytest
from unittest.mock import MagicMock
from provide.terminal.bridge.hub.core import TermHub

@pytest.mark.asyncio
async def test_line_buffering_partial_input():
    hub = TermHub()
    ws = MagicMock()
    
    # Partial input should return None and be stored in buffer
    assert hub._buffer_and_get_command(ws, "ls ") is None
    assert hub._input_buffers[ws] == "ls "

@pytest.mark.asyncio
async def test_line_buffering_complete_command_with_newline():
    hub = TermHub()
    ws = MagicMock()
    
    hub._buffer_and_get_command(ws, "ls ")
    # Completing with \n should return the full command and clear buffer
    assert hub._buffer_and_get_command(ws, "-la\n") == "ls -la\n"
    assert ws not in hub._input_buffers

@pytest.mark.asyncio
async def test_line_buffering_complete_command_with_carriage_return():
    hub = TermHub()
    ws = MagicMock()
    
    hub._buffer_and_get_command(ws, "help")
    # Completing with \r should return the full command and clear buffer
    assert hub._buffer_and_get_command(ws, "\r") == "help\r"
    assert ws not in hub._input_buffers

@pytest.mark.asyncio
async def test_line_buffering_multiple_segments():
    hub = TermHub()
    ws = MagicMock()
    
    assert hub._buffer_and_get_command(ws, "a") is None
    assert hub._buffer_and_get_command(ws, "b") is None
    assert hub._buffer_and_get_command(ws, "c\n") == "abc\n"
    assert ws not in hub._input_buffers

@pytest.mark.asyncio
async def test_line_buffering_multiple_sockets():
    hub = TermHub()
    ws1 = MagicMock()
    ws2 = MagicMock()
    
    assert hub._buffer_and_get_command(ws1, "cmd1") is None
    assert hub._buffer_and_get_command(ws2, "cmd2") is None
    
    assert hub._buffer_and_get_command(ws1, "\n") == "cmd1\n"
    assert hub._input_buffers[ws2] == "cmd2"
    
    assert hub._buffer_and_get_command(ws2, "\r") == "cmd2\r"
    assert ws1 not in hub._input_buffers
    assert ws2 not in hub._input_buffers
