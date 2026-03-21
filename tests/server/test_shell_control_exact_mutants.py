# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Mutation-killing tests for shell connector — exact handle_control assertions."""

from __future__ import annotations

from typing import Any

import pytest

from provide.terminal.server.connectors.shell import ShellSessionConnector


def make(config: dict[str, Any] | None = None) -> ShellSessionConnector:
    return ShellSessionConnector("sess1", "Test Shell", config)


class TestHandleControlExact:
    @pytest.mark.asyncio
    async def test_pause_banner_exact(self):
        """Kills mutmut_6 (None), mutmut_7 (XX...XX), mutmut_8 (lowercase), mutmut_9 (UPPER)."""
        c = make()
        await c.handle_control("pause")
        assert c._banner == "Exclusive control active. Input is still accepted."
        assert c._banner is not None
        assert c._banner != "exclusive control active. input is still accepted."
        assert c._banner != "EXCLUSIVE CONTROL ACTIVE. INPUT IS STILL ACCEPTED."

    @pytest.mark.asyncio
    async def test_pause_appends_system_hijack_acquired_exact(self):
        """Kills mutmut_11 (None), mutmut_14 (XXsystemXX), mutmut_15 (SYSTEM), mutmut_16/17 (text case)."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_control("pause")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        texts = [e.text for e in new_entries]
        assert "system" in speakers
        assert "control: hijack acquired" in texts
        assert None not in texts
        assert "XXsystemXX" not in speakers
        assert "SYSTEM" not in speakers

    @pytest.mark.asyncio
    async def test_resume_paused_is_false_not_none(self):
        """Kills mutmut_21: _paused = None."""
        c = make()
        c._paused = True
        await c.handle_control("resume")
        assert c._paused is False
        assert c._paused is not None

    @pytest.mark.asyncio
    async def test_resume_banner_exact(self):
        """Kills mutmut_23 (None), mutmut_24 (XX...XX), mutmut_25 (lowercase), mutmut_26 (UPPER)."""
        c = make()
        await c.handle_control("resume")
        assert c._banner == "Exclusive control released."
        assert c._banner is not None
        assert c._banner != "exclusive control released."
        assert c._banner != "EXCLUSIVE CONTROL RELEASED."

    @pytest.mark.asyncio
    async def test_resume_appends_system_released_exact(self):
        """Kills mutmut_28 (None), mutmut_31/32 (speaker case), mutmut_33/34 (text case)."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_control("resume")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        texts = [e.text for e in new_entries]
        assert "system" in speakers
        assert "control: released" in texts
        assert None not in texts

    @pytest.mark.asyncio
    async def test_step_case_sensitive(self):
        """Kills mutmut_36 (XXstepXX) and mutmut_37 (STEP)."""
        c = make()
        initial_turns = c._turns
        await c.handle_control("step")
        assert c._turns == initial_turns + 1
        # STEP should go to unknown path
        c2 = make()
        t2 = c2._turns
        await c2.handle_control("STEP")
        # turns not incremented for unknown
        assert c2._turns == t2

    @pytest.mark.asyncio
    async def test_step_increments_by_exactly_one(self):
        """Kills mutmut_38 (=1), mutmut_39 (-=1), mutmut_40 (+=2)."""
        c = make()
        c._turns = 5
        await c.handle_control("step")
        assert c._turns == 6

    @pytest.mark.asyncio
    async def test_step_banner_exact(self):
        """Kills mutmut_41 (None), mutmut_42 (XX...XX), mutmut_43 (lowercase), mutmut_44 (UPPER)."""
        c = make()
        await c.handle_control("step")
        assert c._banner == "Single-step acknowledged."
        assert c._banner is not None
        assert c._banner != "single-step acknowledged."
        assert c._banner != "SINGLE-STEP ACKNOWLEDGED."

    @pytest.mark.asyncio
    async def test_step_appends_system_with_turn_exact(self):
        """Kills mutmut_46 (None), mutmut_49 (XXsystemXX), mutmut_50 (SYSTEM)."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_control("step")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        texts = [e.text for e in new_entries]
        assert "system" in speakers
        assert any("control: single step #" in t for t in texts if t)
        assert None not in texts

    @pytest.mark.asyncio
    async def test_unknown_control_banner_exact(self):
        """Kills mutmut_51: _banner = None."""
        c = make()
        await c.handle_control("unknown_action")
        assert c._banner is not None
        assert "Ignored unknown control action" in c._banner
        assert "unknown_action" in c._banner

    @pytest.mark.asyncio
    async def test_unknown_control_appends_system_exact(self):
        """Kills mutmut_53 (None), mutmut_56 (XXsystemXX), mutmut_57 (SYSTEM)."""
        c = make()
        pre_len = len(c._transcript)
        await c.handle_control("weird")
        entries = list(c._transcript)
        new_entries = entries[pre_len:]
        speakers = [e.speaker for e in new_entries]
        texts = [e.text for e in new_entries]
        assert "system" in speakers
        assert any("control: ignored weird" in t for t in texts if t)
        assert None not in texts


# ---------------------------------------------------------------------------
# get_analysis – exact format checks
# ---------------------------------------------------------------------------
