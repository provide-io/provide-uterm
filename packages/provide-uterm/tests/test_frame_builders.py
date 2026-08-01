#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import pytest

from provide.uterm import frames
from provide.uterm.bridge import schemas


def test_all_core_builders_validate_against_schema() -> None:
    schemas.IdentityFrame.model_validate(frames.make_identity("user:alice"))
    schemas.SessionTokenFrame.model_validate(frames.make_session_token("tok", player_id=1))
    schemas.ResumeFrame.model_validate(frames.make_resume("resume", player_id=2))
    schemas.ResumeOkFrame.model_validate(frames.make_resume_ok())
    schemas.ResumeFailedFrame.model_validate(frames.make_resume_failed("expired"))
    schemas.LinkPatternsFrame.model_validate(frames.make_link_patterns([{"pattern": "sector", "action": "cmd"}]))
    schemas.PresenceUpdateFrame.model_validate(frames.make_presence_update("user-1", scroll_line=10))


def test_builder_facade_exposes_full_core_set() -> None:
    for name in (
        "make_identity",
        "make_link_patterns",
        "make_presence_update",
        "make_resume",
        "make_resume_failed",
        "make_resume_ok",
        "make_session_token",
        "make_snapshot_frame",
    ):
        assert hasattr(frames, name)


def test_make_snapshot_frame_validates_against_schema() -> None:
    frame = frames.make_snapshot_frame(
        screen="screen",
        cursor={"x": 1, "y": 2},
        cols=80,
        rows=24,
        screen_hash="abc",
        cursor_at_end=True,
        has_trailing_space=False,
        prompt_detected=None,
        ts=123.0,
        event_seq=7,
    )

    schemas.SnapshotFrame.model_validate(frame)
    assert frame["prompt_detected"] is None
    assert frame["event_seq"] == 7


def test_invalid_session_token_still_raises() -> None:
    with pytest.raises(ValueError, match="token"):
        frames.make_session_token("")
