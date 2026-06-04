#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for the security-hardening helpers in ``recording``.

``test_recording_local.py`` / ``test_recording_stores.py`` exercise the
store behaviour but cannot pin the *syscall arguments* that carry the security
intent — the directory/file modes (re-tightened by ``chmod``/``fchmod``, so the
final state is identical regardless), the ``O_NOFOLLOW`` flag, the ``S_ISREG``
rejection, and the explicit ``utf-8`` encoding. This suite wraps the syscalls
to assert those exact arguments (a wrapped ``MagicMock`` delegates to the real
call so behaviour is preserved) and adds behavioural symlink/non-regular tests.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from provide.uterm import recording
from provide.uterm.recording import (
    InMemoryRecordingStore,
    LocalFileRecordingStore,
    _ensure_owner_only_dir,
    _open_append_owner_only,
)

_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW


# == _ensure_owner_only_dir ==================================================


def test_ensure_owner_only_dir_creates_and_retightens_0o700(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """mkdir is asked for mode 0o700 AND chmod re-tightens to 0o700.

    Pins the mkdir ``mode`` (removed / 0o701) and the explicit ``chmod(0o700)``.
    """
    mkdir = MagicMock()
    chmod = MagicMock()
    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "chmod", chmod)

    _ensure_owner_only_dir(tmp_path / "rec")

    mkdir.assert_called_once_with(mode=0o700, parents=True, exist_ok=True)
    chmod.assert_called_once_with(0o700)


# == _open_append_owner_only =================================================


def test_open_uses_nofollow_append_and_owner_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The fd is opened O_NOFOLLOW|O_CREAT|O_WRONLY|O_APPEND, mode 0o600, fchmod
    0o600, and wrapped for append in utf-8 — pinning every security-bearing arg.
    """
    path = tmp_path / "s.jsonl"
    opener = MagicMock(wraps=os.open)
    chmoder = MagicMock(wraps=os.fchmod)
    fdopener = MagicMock(wraps=os.fdopen)
    monkeypatch.setattr(recording.os, "open", opener)
    monkeypatch.setattr(recording.os, "fchmod", chmoder)
    monkeypatch.setattr(recording.os, "fdopen", fdopener)

    f = _open_append_owner_only(path)
    try:
        opener.assert_any_call(path, _OPEN_FLAGS, 0o600)  # flags (kills `&`) + create mode
        assert chmoder.call_args.args[1] == 0o600  # re-tighten mode
        assert fdopener.call_args.args[1] == "a"  # append
        assert fdopener.call_args.kwargs.get("encoding") == "utf-8"  # exact encoding
    finally:
        f.close()


def test_open_refuses_symlink_target(tmp_path: Path) -> None:
    """A symlink at the recording path is refused (O_NOFOLLOW → OSError)."""
    real = tmp_path / "real.jsonl"
    real.write_text("", encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    with pytest.raises(OSError):
        _open_append_owner_only(link)


def test_open_refuses_non_regular_sink_naming_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-regular sink is rejected with an OSError that names the path.

    Pins ``OSError(f"…{path}")`` (vs ``OSError(None)``) and the ``os.close(fd)``
    cleanup (vs ``os.close(None)`` → a TypeError that pytest.raises(OSError)
    would not catch).
    """
    path = tmp_path / "s.jsonl"
    monkeypatch.setattr(recording.os, "fstat", lambda _fd: SimpleNamespace(st_mode=stat.S_IFIFO))
    with pytest.raises(OSError) as excinfo:
        _open_append_owner_only(path)
    assert str(path) in str(excinfo.value)


# == get_entries (utf-8 read) ================================================


async def test_get_entries_reads_utf8_tail_and_offset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Both the tail and the offset read paths open the file as utf-8.

    Wraps ``Path.open`` to record the encoding so the exact ``"utf-8"`` literal
    is pinned (None → locale; "UTF-8" → wrong literal).
    """
    store = LocalFileRecordingStore(tmp_path)
    await store.start_session("sess", {"k": "v"})
    await store.append_events("sess", [{"ts": 1.0, "event": "e", "data": {"x": 1}}])
    await store.end_session("sess")

    real_open = Path.open
    encodings: list[str | None] = []

    def _wrapped(self: Path, *args: object, **kwargs: object) -> object:
        encodings.append(kwargs.get("encoding"))  # type: ignore[arg-type]
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _wrapped)

    tail = await store.get_entries("sess")  # offset=None → tail path
    offset = await store.get_entries("sess", offset=0)  # offset path
    assert tail and offset  # both read something
    assert encodings  # the reads happened through the wrapper
    assert all(enc == "utf-8" for enc in encodings)  # exact literal, both branches


async def test_in_memory_store_get_entries_basic() -> None:
    """Cheap parity check that the in-memory store paginates without file I/O."""
    store = InMemoryRecordingStore()
    await store.start_session("s", {})
    await store.append_events("s", [{"ts": 1.0, "event": "e", "data": {}}])
    assert len(await store.get_entries("s")) >= 1
