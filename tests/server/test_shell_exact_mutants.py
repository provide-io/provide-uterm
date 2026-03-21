# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Mutation-killing tests for shell connector — exact init, mode, render assertions."""

from __future__ import annotations

from typing import Any

from provide.terminal.server.connectors.shell import ShellSessionConnector


def make(config: dict[str, Any] | None = None) -> ShellSessionConnector:
    return ShellSessionConnector("sess1", "Test Shell", config)


class TestInitExact:
    def test_connected_false_not_none(self):
        """Kills mutmut_10: _connected = None (is False, not None)."""
        c = make()
        assert c._connected is False
        assert c._connected is not None

    def test_input_mode_is_string_open(self):
        """Kills mutmut_12/13/15/20/21: various wrong default values."""
        c = make()
        assert c._input_mode == "open"
        assert c._input_mode is not None
        assert c._input_mode != "None"
        assert c._input_mode != "XXopenXX"
        assert c._input_mode != "OPEN"

    def test_input_mode_key_is_input_mode(self):
        """Kills mutmut_14 (key=None) / mutmut_18 (XXinput_modeXX) / mutmut_19 (INPUT_MODE)."""
        c = make({"input_mode": "hijack"})
        assert c._input_mode == "hijack"
        # If key was wrong, it would return the default "open"
        c2 = make({"input_mode": "open"})
        assert c2._input_mode == "open"

    def test_input_mode_default_not_none_string(self):
        """Kills mutmut_15 (default=None → str(None) = 'None') / mutmut_17 (no default)."""
        c = make({})
        assert c._input_mode == "open"
        assert c._input_mode != "None"

    def test_input_mode_key_swap_detected(self):
        """Kills mutmut_16: cfg.get('open') instead of cfg.get('input_mode', 'open')."""
        # With key='open', a dict with 'input_mode':'hijack' would still return None (no 'open' key)
        c = make({"input_mode": "hijack"})
        assert c._input_mode == "hijack"


# ---------------------------------------------------------------------------
# _reset_state – exact value checks for all surviving mutants
# ---------------------------------------------------------------------------
class TestResetStateExact:
    def test_paused_is_exactly_false_not_none_not_true(self):
        """Kills mutmut_1 (None) and mutmut_2 (True)."""
        c = make()
        assert c._paused is False
        assert c._paused is not None
        assert c._paused is not True

    def test_turns_is_exactly_zero_not_one(self):
        """Kills mutmut_4: _turns = 1."""
        c = make()
        assert c._turns == 0
        assert c._turns != 1

    def test_nickname_is_exactly_user_not_none_not_uppercase(self):
        """Kills mutmut_5 (None), mutmut_6 (XXuserXX), mutmut_7 (USER)."""
        c = make()
        assert c._nickname == "user"
        assert c._nickname is not None
        assert c._nickname != "USER"
        assert c._nickname != "XXuserXX"

    def test_last_command_is_none_not_empty_string(self):
        """Kills mutmut_8: _last_command = ''."""
        c = make()
        assert c._last_command is None
        assert c._last_command != ""

    def test_banner_is_exactly_ready_help(self):
        """Kills mutmut_9 (None), mutmut_10 (XXReadyXX), mutmut_11 (lowercase), mutmut_12 (UPPER)."""
        c = make()
        assert c._banner == "Ready. Type /help for commands."
        assert c._banner is not None
        assert c._banner != "ready. type /help for commands."
        assert c._banner != "READY. TYPE /HELP FOR COMMANDS."
        assert c._banner != "XXReady. Type /help for commands.XX"

    def test_transcript_maxlen_is_exactly_10_not_none_not_11(self):
        """Kills mutmut_15 (maxlen=None) and mutmut_40 (maxlen=11)."""
        c = make()
        assert c._transcript.maxlen == 10
        assert c._transcript.maxlen is not None
        assert c._transcript.maxlen != 11

    def test_transcript_not_empty_after_reset(self):
        """Kills mutmut_16: initial list removed → empty deque."""
        c = make()
        assert len(c._transcript) == 2

    def test_transcript_has_maxlen_kwarg(self):
        """Kills mutmut_17: maxlen= keyword removed."""
        c = make()
        assert c._transcript.maxlen == 10

    def test_first_entry_text_is_exactly_session_online(self):
        """Kills mutmut_19 (None), mutmut_26 (XXSession online.XX), mutmut_27 (lowercase), mutmut_28 (UPPER)."""
        c = make()
        entries = list(c._transcript)
        assert entries[0].text == "Session online."
        assert entries[0].text is not None
        assert entries[0].text != "session online."
        assert entries[0].text != "SESSION ONLINE."
        assert entries[0].text != "XXSession online.XX"

    def test_first_entry_ts_is_float_not_none(self):
        """Kills mutmut_20: _Entry('system', 'Session online.', None)."""
        c = make()
        entries = list(c._transcript)
        assert entries[0].ts is not None
        assert isinstance(entries[0].ts, float)
        assert entries[0].ts > 0

    def test_first_entry_speaker_is_system_not_uppercase(self):
        """Kills mutmut_24 (XXsystemXX), mutmut_25 (SYSTEM)."""
        c = make()
        entries = list(c._transcript)
        assert entries[0].speaker == "system"
        assert entries[0].speaker != "SYSTEM"
        assert entries[0].speaker != "XXsystemXX"

    def test_second_entry_text_exact(self):
        """Kills mutmut_30 (None), mutmut_37 (XX...XX), mutmut_38 (lowercase), mutmut_39 (UPPER)."""
        c = make()
        entries = list(c._transcript)
        assert entries[1].text == "Use /help, /mode open, /mode hijack, /clear, /status, /reset."
        assert entries[1].text is not None
        assert entries[1].text != "use /help, /mode open, /mode hijack, /clear, /status, /reset."
        assert entries[1].text != "USE /HELP, /MODE OPEN, /MODE HIJACK, /CLEAR, /STATUS, /RESET."

    def test_second_entry_ts_is_float_not_none(self):
        """Kills mutmut_31: second entry ts=None."""
        c = make()
        entries = list(c._transcript)
        assert entries[1].ts is not None
        assert isinstance(entries[1].ts, float)

    def test_second_entry_speaker_exact(self):
        """Kills mutmut_35 (XXsessionXX), mutmut_36 (SESSION)."""
        c = make()
        entries = list(c._transcript)
        assert entries[1].speaker == "session"
        assert entries[1].speaker != "SESSION"
        assert entries[1].speaker != "XXsessionXX"

    def test_reset_restores_defaults(self):
        """Kills mutations that break _reset_state when called explicitly."""
        c = make()
        c._turns = 10
        c._nickname = "changed"
        c._paused = True
        c._reset_state()
        assert c._turns == 0
        assert c._nickname == "user"
        assert c._paused is False
        assert c._banner == "Ready. Type /help for commands."
        assert len(c._transcript) == 2
        assert c._transcript.maxlen == 10


# ---------------------------------------------------------------------------
# _append – exact ts check
# ---------------------------------------------------------------------------
class TestAppendExact:
    def test_append_ts_is_not_none_is_positive_float(self):
        """Kills mutmut_4: _Entry(speaker, text, None) instead of time.time()."""
        c = make()
        c._append("test_speaker", "test_text")
        entry = list(c._transcript)[-1]
        assert entry.ts is not None
        assert isinstance(entry.ts, float)
        assert entry.ts > 1_000_000_000  # Epoch seconds past year 2001


# ---------------------------------------------------------------------------
# _mode_label – exact string checks
# ---------------------------------------------------------------------------
class TestModeLabelExact:
    def test_open_mode_label_exact(self):
        """Kills mutmut_1: 'XXShared inputXX'."""
        c = make({"input_mode": "open"})
        assert c._mode_label() == "Shared input"
        assert c._mode_label() != "XXShared inputXX"

    def test_hijack_mode_label_exact(self):
        """Kills mutmut_7 (XXExclusive hijackXX), mutmut_8 (lowercase), mutmut_9 (UPPER)."""
        c = make({"input_mode": "hijack"})
        assert c._mode_label() == "Exclusive hijack"
        assert c._mode_label() != "XXExclusive hijackXX"
        assert c._mode_label() != "exclusive hijack"
        assert c._mode_label() != "EXCLUSIVE HIJACK"


# ---------------------------------------------------------------------------
# _control_label – exact string checks
# ---------------------------------------------------------------------------
class TestControlLabelExact:
    def test_paused_label_exact(self):
        """Kills mutmut_1: 'XXPaused for hijackXX'."""
        c = make()
        c._paused = True
        assert c._control_label() == "Paused for hijack"
        assert c._control_label() != "XXPaused for hijackXX"

    def test_live_label_exact(self):
        """Kills mutmut_4: 'XXLiveXX'."""
        c = make()
        c._paused = False
        assert c._control_label() == "Live"
        assert c._control_label() != "XXLiveXX"


# ---------------------------------------------------------------------------
# _render_screen – exact content checks
# ---------------------------------------------------------------------------
class TestRenderScreenExact:
    def test_separator_is_exactly_60_dashes(self):
        """Kills mutmut_3 (XX-XX*60) and mutmut_4 ('-'*61)."""
        c = make()
        screen = c._render_screen()
        assert "-" * 60 in screen
        assert "-" * 61 not in screen
        assert "XX-XX" not in screen

    def test_help_line_exact_case(self):
        """Kills mutmut_6 (help: lowercase) and mutmut_7 (UPPER)."""
        c = make()
        screen = c._render_screen()
        assert "Help:" in screen
        assert "help:" not in screen
        assert "HELP:" not in screen

    def test_transcript_header_exact_case(self):
        """Kills mutmut_10 (transcript lowercase) and mutmut_11 (UPPER)."""
        c = make()
        screen = c._render_screen()
        assert "Transcript" in screen
        assert "transcript" not in screen or "Transcript" in screen
        # Check the exact line
        lines = screen.splitlines()
        transcript_lines = [ln for ln in lines if "Transcript" in ln or "transcript" in ln]
        # Must have exactly "Transcript" (not "transcript")
        assert any("Transcript" in ln for ln in transcript_lines)

    def test_empty_line_between_banner_and_transcript(self):
        """Kills mutmut_8: empty string replaced with 'XXXX'."""
        c = make()
        screen = c._render_screen()
        assert "XXXX" not in screen
        lines = screen.splitlines()
        # There should be an empty line
        assert "" in lines

    def test_join_separator_is_newline(self):
        """Kills mutmut_17: '\\n'.join → 'XX\\nXX'.join."""
        c = make()
        screen = c._render_screen()
        assert "XX\nXX" not in screen
        # The screen should be a normal newline-joined string
        assert "\n" in screen

    def test_prompt_line_is_empty_before_it(self):
        """Kills mutmut_14: lines.append('') → lines.append('XXXX')."""
        c = make()
        screen = c._render_screen()
        lines = screen.splitlines()
        # The line before the prompt (last line) should be empty
        if len(lines) >= 2:
            assert lines[-2] == ""
        assert "XXXX" not in screen

    def test_transcript_entries_rendered(self):
        """Kills mutations removing transcript from render."""
        c = make()
        c._append("system", "unique_marker_text")
        screen = c._render_screen()
        assert "unique_marker_text" in screen


# ---------------------------------------------------------------------------
# _snapshot – exact key/value checks
# ---------------------------------------------------------------------------
