# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Mutation-killing tests for shell connector — exact analysis, clear, set_mode assertions."""

from __future__ import annotations

from typing import Any

import pytest

from provide.terminal.server.connectors.shell import ShellSessionConnector


def make(config: dict[str, Any] | None = None) -> ShellSessionConnector:
    return ShellSessionConnector("sess1", "Test Shell", config)


class TestGetAnalysisExact:
    @pytest.mark.asyncio
    async def test_get_analysis_uses_newline_join(self):
        """Kills mutmut_2: 'XX\\nXX'.join instead of '\\n'.join."""
        c = make()
        analysis = await c.get_analysis()
        assert "XX\nXX" not in analysis
        lines = analysis.split("\n")
        assert len(lines) >= 7

    @pytest.mark.asyncio
    async def test_get_analysis_last_command_none_shows_none_text(self):
        """Kills mutmut_3: 'and' instead of 'or' — when last_command is None."""
        c = make()
        analysis = await c.get_analysis()
        assert "last_command: (none)" in analysis

    @pytest.mark.asyncio
    async def test_get_analysis_last_command_none_exact_fallback(self):
        """Kills mutmut_4 (XX(none)XX) and mutmut_5 ((NONE))."""
        c = make()
        analysis = await c.get_analysis()
        assert "last_command: (none)" in analysis
        assert "XX(none)XX" not in analysis
        assert "(NONE)" not in analysis

    @pytest.mark.asyncio
    async def test_get_analysis_last_command_with_value(self):
        """Kills mutmut_3 (and instead of or): when last_command is set, show it."""
        c = make()
        await c.handle_input("/help")
        analysis = await c.get_analysis()
        assert "last_command: /help" in analysis
        assert "last_command: (none)" not in analysis

    @pytest.mark.asyncio
    async def test_get_analysis_prompt_visible_is_true(self):
        """Kills mutmut_6: bool(None) instead of bool(self._prompt().strip())."""
        c = make()
        analysis = await c.get_analysis()
        assert "prompt_visible: True" in analysis
        assert "prompt_visible: False" not in analysis


# ---------------------------------------------------------------------------
# clear – exact checks
# ---------------------------------------------------------------------------
class TestClearExact:
    @pytest.mark.asyncio
    async def test_clear_maxlen_is_10_not_none_not_11(self):
        """Kills mutmut_2 (maxlen=None) and mutmut_3 (maxlen=11)."""
        c = make()
        await c.clear()
        assert c._transcript.maxlen == 10
        assert c._transcript.maxlen is not None
        assert c._transcript.maxlen != 11

    @pytest.mark.asyncio
    async def test_clear_banner_exact(self):
        """Kills mutmut_4 (None), mutmut_5 (XX...XX), mutmut_6 (lowercase), mutmut_7 (UPPER)."""
        c = make()
        await c.clear()
        assert c._banner == "Transcript cleared."
        assert c._banner is not None
        assert c._banner != "XXTranscript cleared.XX"
        assert c._banner != "transcript cleared."
        assert c._banner != "TRANSCRIPT CLEARED."

    @pytest.mark.asyncio
    async def test_clear_empties_transcript(self):
        """Verify clear actually removes entries."""
        c = make()
        c._append("user", "test")
        assert len(c._transcript) > 0
        await c.clear()
        assert len(c._transcript) == 0


# ---------------------------------------------------------------------------
# set_mode – exact checks
# ---------------------------------------------------------------------------
class TestSetModeExact:
    @pytest.mark.asyncio
    async def test_set_mode_open_paused_is_false_not_none(self):
        """Kills mutmut_11: _paused = None."""
        c = make({"input_mode": "hijack"})
        c._paused = True
        await c.set_mode("open")
        assert c._paused is False
        assert c._paused is not None

    @pytest.mark.asyncio
    async def test_set_mode_banner_not_none(self):
        """Kills mutmut_13: _banner = None."""
        c = make()
        await c.set_mode("open")
        assert c._banner is not None
        assert "Input mode set to" in c._banner

    @pytest.mark.asyncio
    async def test_set_mode_appends_system_text_not_none(self):
        """Kills mutmut_15: _append('system', None)."""
        c = make()
        pre_len = len(c._transcript)
        await c.set_mode("open")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        texts = [e.text for e in new_entries]
        assert None not in texts
        assert any(t and "mode:" in t for t in texts)

    @pytest.mark.asyncio
    async def test_set_mode_appends_system_speaker_exact(self):
        """Kills mutmut_18 (XXsystemXX) and mutmut_19 (SYSTEM)."""
        c = make()
        pre_len = len(c._transcript)
        await c.set_mode("open")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        assert "system" in speakers
        assert "XXsystemXX" not in speakers
        assert "SYSTEM" not in speakers
