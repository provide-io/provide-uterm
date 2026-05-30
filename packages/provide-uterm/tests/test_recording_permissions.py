#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import stat

from provide.uterm.recording import LocalFileRecordingStore
from provide.uterm.session_logger import SessionLogger


async def test_recording_file_is_owner_only(tmp_path) -> None:
    store = LocalFileRecordingStore(tmp_path)
    await store.start_session("sess1", {"k": "v"})
    path = tmp_path / "sess1.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_recording_append_lazy_open_is_owner_only(tmp_path) -> None:
    """append_events lazy-open branch (no prior start_session) must also chmod 0o600."""
    store = LocalFileRecordingStore(tmp_path)
    await store.append_events("sess2", [{"ts": 1, "event": "data", "data": "x"}])
    path = tmp_path / "sess2.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_legacy_file_store_start_session_is_owner_only(tmp_path) -> None:
    """LegacyFileStore.start_session (SessionLogger path ctor) must chmod 0o600."""
    log_path = tmp_path / "s.jsonl"
    sl = SessionLogger(log_path)
    await sl.start("session-a")
    await sl.stop()
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


async def test_legacy_file_store_append_events_is_owner_only(tmp_path) -> None:
    """LegacyFileStore.append_events must chmod 0o600 on each open."""
    log_path = tmp_path / "s2.jsonl"
    sl = SessionLogger(log_path)
    await sl.start("session-b")
    # Buffer a real event so flush() actually invokes append_events (and its
    # chmod). Without an event the buffer is empty and _flush_buffer_unlocked
    # early-returns, so append_events would never run.
    await sl.log_send("a")
    await sl.flush()
    await sl.stop()
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
