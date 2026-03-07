#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for the interactive demo session helpers."""

from __future__ import annotations

from tests.conftest import load_demo_server_module

demo = load_demo_server_module()


def _fresh(worker_id: str = "unit-session") -> demo.DemoSessionState:
    demo._reset_all_sessions()
    return demo._get_or_create_session(worker_id)


def test_free_form_input_appends_transcript_and_reply() -> None:
    session = _fresh()

    messages = demo._apply_input(session, "hello there")

    assert len(messages) == 1
    assert messages[0]["type"] == "snapshot"
    assert session.transcript[-2].text == "user: hello there"
    assert session.transcript[-1].text == 'session: received "hello there"'
    assert session.pending_banner == "Input accepted."


def test_help_command_adds_help_text() -> None:
    session = _fresh()

    demo._apply_input(session, "/help")

    assert "Commands:" in session.transcript[-1].text
    assert session.last_command == "/help"


def test_clear_command_empties_transcript() -> None:
    session = _fresh()
    demo._apply_input(session, "first line")

    demo._apply_input(session, "/clear")

    assert session.transcript == []
    assert session.pending_banner == "Transcript cleared."


def test_mode_command_switches_to_open_and_emits_worker_hello() -> None:
    session = _fresh()

    messages = demo._apply_input(session, "/mode open")

    assert session.input_mode == "open"
    assert messages[0]["type"] == "worker_hello"
    assert messages[0]["input_mode"] == "open"
    assert messages[1]["type"] == "snapshot"


def test_pause_does_not_disable_input_handling() -> None:
    session = _fresh()

    demo._apply_control(session, "pause")
    demo._apply_input(session, "still works")

    assert session.paused is True
    assert session.transcript[-2].text == "user: still works"
    assert session.transcript[-1].text == 'session: received "still works"'


def test_resume_clears_paused_state() -> None:
    session = _fresh()
    demo._apply_control(session, "pause")

    demo._apply_control(session, "resume")

    assert session.paused is False
    assert session.status_line == "Live"
    assert session.transcript[-1].text == "control: released"


def test_step_appends_marker_and_increments_turns() -> None:
    session = _fresh()

    demo._apply_control(session, "step")

    assert session.turn_counter == 1
    assert "single step #1" in session.transcript[-1].text


def test_analysis_reflects_live_state() -> None:
    session = _fresh()
    demo._apply_input(session, "/nick tester")
    demo._apply_input(session, "ping")

    analysis = demo._make_analysis(session)

    assert "input_mode: hijack" in analysis
    assert "paused: False" in analysis
    assert "turn_counter: 2" in analysis
    assert "analysis_note: free-form input received" in analysis


def test_rendered_snapshot_contains_mode_status_and_prompt() -> None:
    session = _fresh()
    demo._apply_input(session, "/mode open")

    snapshot = demo._make_snapshot(session)

    assert "Mode:" in snapshot["screen"]
    assert "Shared input" in snapshot["screen"]
    assert "Control:" in snapshot["screen"]
    assert "user>" in snapshot["screen"]


def test_reset_command_replaces_transcript_with_fresh_state() -> None:
    session = _fresh()
    demo._apply_input(session, "before reset")

    messages = demo._apply_input(session, "/reset")
    refreshed = demo._get_or_create_session(session.worker_id)

    assert messages[0]["type"] == "worker_hello"
    assert refreshed.transcript[0].text == "Session online."
    assert refreshed.pending_banner == "Session reset."
