#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Discoverable validating frame-builder facade."""

from __future__ import annotations

import time
from typing import Any

from provide.uterm.bridge.schemas import SnapshotFrame
from provide.uterm.control_channel_builders import (
    make_identity,
    make_link_patterns,
    make_presence_update,
    make_resume,
    make_resume_failed,
    make_resume_ok,
    make_session_token,
)


def make_snapshot_frame(
    *,
    screen: str,
    cursor: dict[str, int],
    cols: int,
    rows: int,
    screen_hash: str,
    cursor_at_end: bool,
    has_trailing_space: bool,
    prompt_detected: dict[str, Any] | None,
    ts: float | None = None,
    raw_tail: str | None = None,
    event_seq: int | None = None,
) -> dict[str, Any]:
    """Build a validated snapshot frame."""
    frame = SnapshotFrame(
        type="snapshot",
        screen=screen,
        cursor=cursor,
        cols=cols,
        rows=rows,
        screen_hash=screen_hash,
        cursor_at_end=cursor_at_end,
        has_trailing_space=has_trailing_space,
        prompt_detected=prompt_detected,
        raw_tail=raw_tail,
        ts=time.time() if ts is None else ts,
        event_seq=event_seq,
    ).model_dump(exclude_none=False)
    if event_seq is None:
        frame.pop("event_seq")
    return frame


__all__ = [
    "make_identity",
    "make_link_patterns",
    "make_presence_update",
    "make_resume",
    "make_resume_failed",
    "make_resume_ok",
    "make_session_token",
    "make_snapshot_frame",
]
