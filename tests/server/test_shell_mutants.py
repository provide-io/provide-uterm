# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Mutation-killing tests for ShellSessionConnector."""

from __future__ import annotations

from typing import Any

import pytest

from provide.terminal.server.connectors.shell import ShellSessionConnector


def make(config: dict[str, Any] | None = None) -> ShellSessionConnector:
    return ShellSessionConnector("sess1", "Test Shell", config)


# ---------------------------------------------------------------------------
# __init__ mutations
# ---------------------------------------------------------------------------
class TestInitMutations:
    def test_connected_is_false_after_init(self):
        """Kills mutmut_10: _connected = None."""
        c = make()
        assert c._connected is False
        assert c._connected == False  # noqa: E712

    def test_input_mode_default_is_open(self):
        """Kills mutmut_12/13/14/15: _input_mode default is 'open'."""
        c = make()
        assert c._input_mode == "open"

    def test_input_mode_from_config(self):
        """Kills mutmut_14: cfg.get(None, 'open') — key becomes None."""
        c = make({"input_mode": "hijack"})
        assert c._input_mode == "hijack"

    def test_input_mode_default_none_config(self):
        """Kills mutmut_15: cfg.get('input_mode', None) → None returned."""
        c = make({})  # No input_mode key → should default to "open"
        assert c._input_mode == "open"
        assert c._input_mode is not None

    def test_unknown_config_keys_raises(self):
        """Kills mutations to the unknown key check."""
        with pytest.raises(ValueError, match="unknown shell connector_config keys"):
            make({"bogus_key": "value"})

    def test_session_id_stored(self):
        """Kills mutations that skip storing session_id."""
        c = make()
        assert c._session_id == "sess1"

    def test_display_name_stored(self):
        """Kills mutations that skip storing display_name."""
        c = make()
        assert c._display_name == "Test Shell"

    def test_reset_state_called_on_init(self):
        """Kills mutmut_21: _reset_state() not called."""
        c = make()
        # After init, _transcript should exist and be non-empty
        assert hasattr(c, "_transcript")
        assert len(c._transcript) > 0

    def test_valid_config_keys_accepted(self):
        """Tests that valid config keys don't raise."""
        c = make({"input_mode": "open"})
        assert c._input_mode == "open"


# ---------------------------------------------------------------------------
# _reset_state mutations
# ---------------------------------------------------------------------------
class TestResetState:
    def test_paused_is_false(self):
        """Kills mutmut_1/2: _paused = None/True."""
        c = make()
        assert c._paused is False

    def test_turns_is_zero(self):
        """Kills mutmut_3: _turns = None."""
        c = make()
        assert c._turns == 0

    def test_nickname_is_user(self):
        """Kills mutations to _nickname default."""
        c = make()
        assert c._nickname == "user"

    def test_last_command_is_none(self):
        """Kills mutations to _last_command initial value."""
        c = make()
        assert c._last_command is None

    def test_banner_exact_text(self):
        """Kills mutmut_10/11/12: banner text mutations."""
        c = make()
        assert c._banner == "Ready. Type /help for commands."

    def test_transcript_has_two_initial_entries(self):
        """Kills mutations to transcript initialization."""
        c = make()
        assert len(c._transcript) == 2

    def test_transcript_first_entry_speaker(self):
        """Kills mutmut_15/16: first entry speaker mutations."""
        c = make()
        entries = list(c._transcript)
        assert entries[0].speaker == "system"

    def test_transcript_first_entry_text(self):
        """Kills mutmut_17: first entry text mutation."""
        c = make()
        entries = list(c._transcript)
        assert entries[0].text == "Session online."

    def test_transcript_second_entry_speaker(self):
        """Kills mutmut_19: second entry speaker mutation."""
        c = make()
        entries = list(c._transcript)
        assert entries[1].speaker == "session"

    def test_transcript_maxlen_is_10(self):
        """Kills mutations to maxlen value."""
        c = make()
        assert c._transcript.maxlen == 10


# ---------------------------------------------------------------------------
# stop mutation (mutmut_1: _connected = None instead of False)
# ---------------------------------------------------------------------------
class TestStopConnectedFalse:
    @pytest.mark.asyncio
    async def test_stop_sets_connected_to_false(self):
        """Kills mutmut_1: _connected = None."""
        c = make()
        await c.start()
        assert c.is_connected()
        await c.stop()
        assert c._connected is False
        assert not c.is_connected()


# ---------------------------------------------------------------------------
# _mode_label mutations
# ---------------------------------------------------------------------------
class TestModeLabel:
    def test_open_mode_label_exact_text(self):
        """Kills mutmut_1/2/3: label text mutations for open mode."""
        c = make({"input_mode": "open"})
        label = c._mode_label()
        assert label == "Shared input"

    def test_hijack_mode_label_exact_text(self):
        """Kills mutmut_7/8/9: label text mutations for hijack mode."""
        c = make({"input_mode": "hijack"})
        label = c._mode_label()
        assert label == "Exclusive hijack"

    def test_non_open_mode_shows_hijack_label(self):
        """Kills mutmut_4: condition inverted (== vs !=)."""
        c = make({"input_mode": "hijack"})
        assert c._mode_label() == "Exclusive hijack"
        c2 = make({"input_mode": "open"})
        assert c2._mode_label() == "Shared input"


# ---------------------------------------------------------------------------
# _control_label mutations
# ---------------------------------------------------------------------------
class TestControlLabel:
    def test_paused_true_shows_paused_for_hijack(self):
        """Kills mutmut_1/2/3: 'Paused for hijack' text mutations."""
        c = make()
        c._paused = True
        assert c._control_label() == "Paused for hijack"

    def test_paused_false_shows_live(self):
        """Kills mutmut_4/5/6: 'Live' text mutations."""
        c = make()
        c._paused = False
        assert c._control_label() == "Live"


# ---------------------------------------------------------------------------
# _hello mutations
# ---------------------------------------------------------------------------
class TestHello:
    def test_hello_has_ts_key(self):
        """Kills mutmut_7/8: 'ts' key mutated to 'XXtsXX'/'TS'."""
        c = make()
        hello = c._hello()
        assert "ts" in hello
        assert isinstance(hello["ts"], float)

    def test_hello_type_is_worker_hello(self):
        """Kills type key mutations."""
        c = make()
        hello = c._hello()
        assert hello["type"] == "worker_hello"

    def test_hello_input_mode_matches(self):
        """Kills input_mode mutations."""
        c = make({"input_mode": "hijack"})
        hello = c._hello()
        assert hello["input_mode"] == "hijack"


# ---------------------------------------------------------------------------
# _render_screen mutations
# ---------------------------------------------------------------------------
class TestRenderScreen:
    def test_separator_is_60_dashes(self):
        """Kills mutmut_3/4: '-' * 60 vs 61 or different string."""
        c = make()
        screen = c._render_screen()
        assert "-" * 60 in screen

    def test_help_text_in_screen(self):
        """Kills mutmut_5/6/7: Help: line mutations."""
        c = make()
        screen = c._render_screen()
        assert "/help" in screen
        assert "Help:" in screen

    def test_transcript_header_in_screen(self):
        """Kills mutmut_8/9/10/11: Transcript header mutations."""
        c = make()
        screen = c._render_screen()
        assert "Transcript" in screen

    def test_screen_includes_session_id(self):
        """Kills mutations removing session_id from header."""
        c = make()
        screen = c._render_screen()
        assert "sess1" in screen

    def test_screen_includes_display_name(self):
        """Kills mutations removing display_name from header."""
        c = make()
        screen = c._render_screen()
        assert "Test Shell" in screen

    def test_mode_label_in_screen(self):
        """Kills _render_screen mutmut_14: mode label missing."""
        c = make({"input_mode": "open"})
        screen = c._render_screen()
        assert "Shared input" in screen

    def test_control_label_in_screen(self):
        """Kills control label mutations in render."""
        c = make()
        screen = c._render_screen()
        assert "Live" in screen

    def test_banner_in_screen(self):
        """Kills banner mutations in render."""
        c = make()
        screen = c._render_screen()
        assert "Ready" in screen

    def test_prompt_at_end(self):
        """Kills mutmut_17 and others: prompt missing/wrong."""
        c = make()
        screen = c._render_screen()
        # Last non-empty line should contain the prompt
        lines = [ln for ln in screen.splitlines() if ln.strip()]
        assert "user>" in lines[-1]

    def test_lines_limited_to_rows(self):
        """Kills mutations changing -_ROWS to something else."""
        c = make()
        screen = c._render_screen()
        # Screen should have at most _ROWS=25 lines
        assert len(screen.splitlines()) <= 25


# ---------------------------------------------------------------------------
# _snapshot mutations
# ---------------------------------------------------------------------------
class TestSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_screen_is_string(self):
        """Kills mutmut_1: screen = None."""
        c = make()
        snap = c._snapshot()
        assert isinstance(snap["screen"], str)
        assert len(snap["screen"]) > 0

    def test_cursor_x_is_integer(self):
        """Kills mutmut_7/8/9/10/11: cursor_x mutations."""
        c = make()
        snap = c._snapshot()
        assert isinstance(snap["cursor"]["x"], int)
        assert 0 <= snap["cursor"]["x"] <= 79  # 0 to _COLS-1

    def test_cursor_y_is_integer(self):
        """Kills mutmut_14: cursor_y = None."""
        c = make()
        snap = c._snapshot()
        assert isinstance(snap["cursor"]["y"], int)
        assert snap["cursor"]["y"] >= 0

    def test_cursor_x_bounded_by_cols_minus_one(self):
        """Kills mutmut_12 (COLS+1) and mutmut_13 (COLS-2)."""
        c = make()
        snap = c._snapshot()
        # cursor_x must be <= _COLS - 1 = 79
        assert snap["cursor"]["x"] <= 79

    def test_cols_is_80(self):
        """Kills mutations to _COLS value."""
        c = make()
        snap = c._snapshot()
        assert snap["cols"] == 80

    def test_rows_is_25(self):
        """Kills mutations to _ROWS value."""
        c = make()
        snap = c._snapshot()
        assert snap["rows"] == 25

    def test_screen_hash_is_16_chars(self):
        """Kills mutations to hash truncation [:16]."""
        c = make()
        snap = c._snapshot()
        assert len(snap["screen_hash"]) == 16

    def test_snapshot_has_required_keys(self):
        """Kills mutations removing keys."""
        c = make()
        snap = c._snapshot()
        for key in [
            "type",
            "screen",
            "cursor",
            "cols",
            "rows",
            "screen_hash",
            "cursor_at_end",
            "has_trailing_space",
            "prompt_detected",
            "ts",
        ]:
            assert key in snap, f"Missing key: {key}"

    def test_cursor_at_end_is_true(self):
        """Kills mutations changing cursor_at_end."""
        c = make()
        snap = c._snapshot()
        assert snap["cursor_at_end"] is True

    def test_has_trailing_space_is_false(self):
        """Kills mutations changing has_trailing_space."""
        c = make()
        snap = c._snapshot()
        assert snap["has_trailing_space"] is False

    def test_prompt_detected_has_prompt_id(self):
        """Kills mutations to prompt_detected."""
        c = make()
        snap = c._snapshot()
        assert snap["prompt_detected"]["prompt_id"] == "shell_prompt"


# ---------------------------------------------------------------------------
# _append mutation (mutmut_4: ts=None)
# ---------------------------------------------------------------------------
class TestAppend:
    def test_append_entry_ts_is_float(self):
        """Kills mutmut_4: _Entry ts=None instead of time.time()."""
        c = make()
        c._append("system", "test message")
        entry = list(c._transcript)[-1]
        assert entry.ts is not None
        assert isinstance(entry.ts, float)
        assert entry.ts > 0


# ---------------------------------------------------------------------------
# handle_input structural mutations
# ---------------------------------------------------------------------------
