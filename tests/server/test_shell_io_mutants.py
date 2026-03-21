# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Mutation-killing tests for shell connector — handle_input, control, set_mode."""

from __future__ import annotations

from typing import Any

import pytest

from provide.terminal.server.connectors.shell import ShellSessionConnector


def make(config: dict[str, Any] | None = None) -> ShellSessionConnector:
    return ShellSessionConnector("sess1", "Test Shell", config)


class TestHandleInputMutations:
    @pytest.mark.asyncio
    async def test_turns_increments_on_input(self):
        """Kills mutmut_8/9: _turns = 1 or _turns -= 1."""
        c = make()
        assert c._turns == 0
        await c.handle_input("hello")
        assert c._turns == 1
        await c.handle_input("world")
        assert c._turns == 2

    @pytest.mark.asyncio
    async def test_turns_not_incremented_on_empty(self):
        """Kills mutations that increment turns on empty."""
        c = make()
        await c.handle_input("   ")
        assert c._turns == 0

    @pytest.mark.asyncio
    async def test_last_command_set_for_slash_commands(self):
        """Kills mutmut_18: _last_command = None instead of command."""
        c = make()
        await c.handle_input("/help")
        assert c._last_command == "/help"

    @pytest.mark.asyncio
    async def test_help_command_exact_match(self):
        """Kills mutmut_20/21: '/help' → 'XX/helpXX'/'/HELP'."""
        c = make()
        msgs = await c.handle_input("/help")
        # /help command must be handled (returns snapshot with help content)
        assert msgs[-1]["type"] == "snapshot"
        # Check banner is set (not just "Unknown command")
        assert "Unknown command" not in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_help_banner_exact_text(self):
        """Kills mutmut_22/23/24/25: banner text mutations."""
        c = make()
        await c.handle_input("/help")
        assert c._banner == "Command help printed below."

    @pytest.mark.asyncio
    async def test_help_appends_system_entry(self):
        """Kills mutmut_27/30/31/32/33/34: _append speaker/text mutations."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_input("/help")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        assert len(new_entries) >= 1
        # Should append with speaker "system"
        system_entries = [e for e in new_entries if e.speaker == "system"]
        assert len(system_entries) >= 1
        # Text should contain "Commands:"
        assert any("Commands:" in e.text for e in system_entries)

    @pytest.mark.asyncio
    async def test_empty_input_banner_text(self):
        """Kills mutmut_5: 'Empty input ignored.' text mutation."""
        c = make()
        await c.handle_input("   ")
        assert c._banner == "Empty input ignored."

    @pytest.mark.asyncio
    async def test_clear_command_exact(self):
        """Kills /clear command mutations."""
        c = make()
        await c.handle_input("before clear")
        len(c._transcript)
        await c.handle_input("/clear")
        assert len(c._transcript) == 0
        assert c._banner == "Transcript cleared."

    @pytest.mark.asyncio
    async def test_mode_command_calls_set_mode(self):
        """Kills mutations routing /mode incorrectly."""
        c = make()
        msgs = await c.handle_input("/mode hijack")
        assert any(m["type"] == "worker_hello" for m in msgs)

    @pytest.mark.asyncio
    async def test_status_command_shows_mode(self):
        """Kills /status mutations."""
        c = make()
        msgs = await c.handle_input("/status")
        assert "mode=" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_nick_command_sets_nickname(self):
        """Kills /nick mutations."""
        c = make()
        msgs = await c.handle_input("/nick testuser")
        assert c._nickname == "testuser"
        assert "testuser" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_nick_truncates_to_24(self):
        """Kills nick length mutations."""
        c = make()
        long_name = "a" * 30
        await c.handle_input(f"/nick {long_name}")
        assert len(c._nickname) == 24

    @pytest.mark.asyncio
    async def test_say_appends_user_entry(self):
        """Kills /say mutations."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_input("/say hello world")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        assert any(e.speaker == "user" for e in new_entries)
        assert any("hello world" in e.text for e in new_entries)

    @pytest.mark.asyncio
    async def test_plain_input_appends_user_and_session(self):
        """Kills mutations to plain input handling."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_input("test input")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = {e.speaker for e in new_entries}
        assert "user" in speakers
        assert "session" in speakers

    @pytest.mark.asyncio
    async def test_plain_input_banner_text(self):
        """Kills 'Input accepted.' banner mutations."""
        c = make()
        await c.handle_input("some text")
        assert c._banner == "Input accepted."

    @pytest.mark.asyncio
    async def test_reset_command_returns_hello_and_snapshot(self):
        """Kills /reset return value mutations."""
        c = make()
        await c.handle_input("some text")
        msgs = await c.handle_input("/reset")
        types = [m["type"] for m in msgs]
        assert "worker_hello" in types
        assert "snapshot" in types
        assert c._turns == 0

    @pytest.mark.asyncio
    async def test_unknown_command_shows_command_name(self):
        """Kills unknown command handling mutations."""
        c = make()
        msgs = await c.handle_input("/unknown_cmd")
        assert "unknown_cmd" in msgs[-1]["screen"]


# ---------------------------------------------------------------------------
# handle_control mutations
# ---------------------------------------------------------------------------
class TestHandleControlMutations:
    @pytest.mark.asyncio
    async def test_pause_sets_paused_true(self):
        """Kills mutations changing pause behavior."""
        c = make()
        await c.handle_control("pause")
        assert c._paused is True

    @pytest.mark.asyncio
    async def test_resume_sets_paused_false(self):
        """Kills mutations changing resume behavior."""
        c = make()
        c._paused = True
        await c.handle_control("resume")
        assert c._paused is False

    @pytest.mark.asyncio
    async def test_step_increments_turns(self):
        """Kills mutations to step handling."""
        c = make()
        initial = c._turns
        await c.handle_control("step")
        assert c._turns == initial + 1

    @pytest.mark.asyncio
    async def test_pause_banner_exact_text(self):
        """Kills mutations to pause banner text."""
        c = make()
        await c.handle_control("pause")
        assert c._banner == "Exclusive control active. Input is still accepted."

    @pytest.mark.asyncio
    async def test_resume_banner_exact_text(self):
        """Kills mutations to resume banner text."""
        c = make()
        await c.handle_control("resume")
        assert c._banner == "Exclusive control released."


# ---------------------------------------------------------------------------
# set_mode mutations
# ---------------------------------------------------------------------------
class TestSetModeMutations:
    @pytest.mark.asyncio
    async def test_set_mode_open_sets_paused_false(self):
        """Kills mutmut_11: _paused = None instead of False."""
        c = make({"input_mode": "hijack"})
        c._paused = True
        await c.set_mode("open")
        assert c._paused is False
        assert c._paused is not None

    @pytest.mark.asyncio
    async def test_set_mode_hijack_does_not_clear_paused(self):
        """Kills mutations that also clear paused on hijack."""
        c = make({"input_mode": "open"})
        # In hijack mode, paused flag should NOT be automatically cleared
        c._paused = True
        await c.set_mode("hijack")
        # paused stays True for hijack mode
        assert c._paused is True

    @pytest.mark.asyncio
    async def test_set_mode_banner_not_none(self):
        """Kills mutmut_13: _banner = None."""
        c = make()
        await c.set_mode("open")
        assert c._banner is not None
        assert "Input mode" in c._banner

    @pytest.mark.asyncio
    async def test_set_mode_appends_system_entry(self):
        """Kills mutmut_15/18/19: _append mutations."""
        c = make()
        pre_len = len(c._transcript)
        await c.set_mode("open")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        assert len(new_entries) >= 1
        speakers = {e.speaker for e in new_entries}
        assert "system" in speakers

    @pytest.mark.asyncio
    async def test_set_mode_updates_input_mode_attribute(self):
        """Kills mutations that don't update _input_mode."""
        c = make({"input_mode": "open"})
        await c.set_mode("hijack")
        assert c._input_mode == "hijack"

    @pytest.mark.asyncio
    async def test_set_mode_returns_hello_with_correct_mode(self):
        """Kills mutations where hello has wrong mode."""
        c = make({"input_mode": "open"})
        msgs = await c.set_mode("hijack")
        hello = next(m for m in msgs if m["type"] == "worker_hello")
        assert hello["input_mode"] == "hijack"


# ---------------------------------------------------------------------------
# __init__ – exact string/type checks for surviving mutants
# ---------------------------------------------------------------------------
