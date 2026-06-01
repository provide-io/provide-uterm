#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import errno
import os
import stat

import pytest

from provide.uterm.recording import LocalFileRecordingStore, _open_append_owner_only
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


# ---------------------------------------------------------------------------
# L12: recording/log file open must be symlink-safe (O_NOFOLLOW) and must
# re-tighten a PRE-EXISTING loose-perm file to 0o600 (fchmod on the fd).
# ---------------------------------------------------------------------------


def test_open_helper_new_path_is_owner_only(tmp_path) -> None:
    """L12: a fresh (non-existing) path is created exactly 0o600 (regression)."""
    path = tmp_path / "fresh.jsonl"
    with _open_append_owner_only(path) as f:
        f.write("x\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_open_helper_retightens_preexisting_loose_file(tmp_path) -> None:
    """L12: a PRE-EXISTING world/group-readable file is re-tightened to 0o600.

    The create-mode arg to ``os.open`` only applies when O_CREAT actually
    creates the file, so a pre-existing 0o644 file would otherwise stay loose.
    The fd-targeted ``fchmod`` must enforce 0o600 on the existing file.
    """
    path = tmp_path / "preexisting.jsonl"
    path.write_text("old\n")
    path.chmod(0o644)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644  # precondition

    with _open_append_owner_only(path) as f:
        f.write("new\n")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Append semantics preserved: existing content is retained.
    assert path.read_text() == "old\nnew\n"


def test_open_helper_refuses_symlink(tmp_path) -> None:
    """L12: a symlink pre-created at the target path must be refused (O_NOFOLLOW
    raises OSError/ELOOP) and must NOT be written through to the link target."""
    target = tmp_path / "attacker_target.txt"
    target.write_text("untouched\n")
    link = tmp_path / "session.jsonl"
    link.symlink_to(target)

    with pytest.raises(OSError) as excinfo:
        _open_append_owner_only(link)
    assert excinfo.value.errno in (errno.ELOOP, errno.EMLINK)

    # The open refused — nothing was written through to the link target.
    assert target.read_text() == "untouched\n"


def test_open_helper_rejects_non_regular_sink_and_closes_fd(tmp_path, monkeypatch) -> None:
    """L12: a non-regular sink (e.g. a fifo/device pre-created at the path) is
    refused, and the just-opened fd is closed so it does not leak.

    A real fifo would block ``os.open(..., O_WRONLY)`` with no reader, so we
    stub ``os.fstat`` to report a fifo mode and assert the regular-file guard
    fires and the cleanup path closes the fd.
    """
    path = tmp_path / "sink.jsonl"
    real_close = os.close
    closed: list[int] = []

    def _spy_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def _fake_fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        # Force the S_IFIFO type bits so S_ISREG(...) is False.
        return os.stat_result((stat.S_IFIFO | 0o600, *tuple(st)[1:]))

    real_fstat = os.fstat
    monkeypatch.setattr(os, "fstat", _fake_fstat)
    monkeypatch.setattr(os, "close", _spy_close)

    with pytest.raises(OSError, match="non-regular recording sink"):
        _open_append_owner_only(path)

    # The fd opened inside the helper was closed by the except-cleanup branch.
    assert closed, "helper must close the fd before re-raising (no fd leak)"


# ---------------------------------------------------------------------------
# L13: the recordings directory must be re-tightened to 0o700 even when it
# ALREADY exists (mkdir's mode only applies on creation).
# ---------------------------------------------------------------------------


async def test_recording_dir_retightened_when_preexisting(tmp_path) -> None:
    """L13: a PRE-EXISTING 0o755 recordings dir is re-chmod-ed to 0o700.

    ``mkdir(..., exist_ok=True)`` is a no-op (mode-wise) on an existing dir, so
    without an explicit follow-up ``chmod`` a group/world-readable recordings
    dir would stay enumerable by other local users.
    """
    rec_dir = tmp_path / "preexisting_dir"
    rec_dir.mkdir(mode=0o755)
    rec_dir.chmod(0o755)
    assert stat.S_IMODE(rec_dir.stat().st_mode) == 0o755  # precondition

    store = LocalFileRecordingStore(rec_dir)
    await store.start_session("sess1", {"k": "v"})

    assert stat.S_IMODE(rec_dir.stat().st_mode) == 0o700


async def test_recording_dir_retightened_when_preexisting_lazy_append(tmp_path) -> None:
    """L13: the lazy-open append path also re-tightens a pre-existing dir."""
    rec_dir = tmp_path / "preexisting_dir_append"
    rec_dir.mkdir(mode=0o755)
    rec_dir.chmod(0o755)
    assert stat.S_IMODE(rec_dir.stat().st_mode) == 0o755  # precondition

    store = LocalFileRecordingStore(rec_dir)
    await store.append_events("sess2", [{"ts": 1, "event": "data", "data": "x"}])

    assert stat.S_IMODE(rec_dir.stat().st_mode) == 0o700
