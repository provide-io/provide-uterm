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


# ---------------------------------------------------------------------------
# L26: no world/group-readable TOCTOU window, and recordings dir is 0o700 so
# session filenames cannot be enumerated by other local users.
# ---------------------------------------------------------------------------


async def test_recording_file_never_world_or_group_readable(tmp_path) -> None:
    """L26: the recording file must carry no world/group bits — it is created
    atomically at 0o600, not opened world-readable then chmod-ed."""
    store = LocalFileRecordingStore(tmp_path / "recdir")
    await store.start_session("sess1", {"k": "v"})
    mode = stat.S_IMODE((tmp_path / "recdir" / "sess1.jsonl").stat().st_mode)
    assert mode == 0o600
    assert mode & 0o077 == 0  # no group/world bits


async def test_recording_directory_is_owner_only(tmp_path) -> None:
    """L26: the recordings directory is 0o700 (was 0o755) so filenames are not
    enumerable by other local users."""
    rec_dir = tmp_path / "recdir2"
    store = LocalFileRecordingStore(rec_dir)
    await store.start_session("sess1", {"k": "v"})
    assert stat.S_IMODE(rec_dir.stat().st_mode) == 0o700


async def test_recording_append_lazy_open_directory_is_owner_only(tmp_path) -> None:
    """L26: the lazy-open append path also creates the dir 0o700 and the file
    0o600 with no group/world bits."""
    rec_dir = tmp_path / "recdir3"
    store = LocalFileRecordingStore(rec_dir)
    await store.append_events("sess2", [{"ts": 1, "event": "data", "data": "x"}])
    file_mode = stat.S_IMODE((rec_dir / "sess2.jsonl").stat().st_mode)
    assert file_mode == 0o600
    assert file_mode & 0o077 == 0


async def test_legacy_file_store_directory_is_owner_only(tmp_path) -> None:
    """L26: the SessionLogger LegacyFileStore creates its parent dir 0o700 and
    its log file 0o600 with no group/world bits."""
    log_dir = tmp_path / "logdir"
    log_path = log_dir / "s.jsonl"
    sl = SessionLogger(log_path)
    await sl.start("session-a")
    await sl.stop()
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    file_mode = stat.S_IMODE(log_path.stat().st_mode)
    assert file_mode == 0o600
    assert file_mode & 0o077 == 0
