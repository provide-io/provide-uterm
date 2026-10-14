#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""SessionLogger.quota_reached: whether anything more would be written."""

from __future__ import annotations

from pathlib import Path

from provide.uterm.session_logger import SessionLogger


def test_quota_reached_once_the_bytes_written_meet_the_quota(tmp_path: Path) -> None:
    logger = SessionLogger(tmp_path / "s.jsonl", max_bytes=10)
    assert not logger.quota_reached
    logger._bytes_written = 10
    assert logger.quota_reached
    # Read without side effects: no warning is spent on asking.
    assert not logger._quota_warned


def test_an_unlimited_recording_is_never_full(tmp_path: Path) -> None:
    logger = SessionLogger(tmp_path / "s.jsonl")
    logger._bytes_written = 10**12
    assert not logger.quota_reached
