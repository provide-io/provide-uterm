#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import os
import stat
import sys

import pytest

from provide.uterm.file_io import secure_create, secure_open_append

# Windows has no POSIX permission bits: st_mode is synthesized purely from the
# read-only attribute, so it never reads back as the exact 0o700/0o600 this
# package requests. Exact-mode assertions are POSIX-only; Windows coverage
# below checks the write succeeds instead.
_EXACT_MODE_BITS_SUPPORTED = sys.platform != "win32"


def test_secure_open_append_creates_owner_only_file_and_parent(tmp_path) -> None:
    path = tmp_path / "logs" / "session.jsonl"

    with secure_open_append(path) as handle:
        handle.write("one\n")

    if _EXACT_MODE_BITS_SUPPORTED:
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "one\n"


def test_secure_open_append_appends_without_truncating(tmp_path) -> None:
    path = tmp_path / "session.jsonl"

    with secure_open_append(path) as handle:
        handle.write("one\n")
    with secure_open_append(path) as handle:
        handle.write("two\n")

    assert path.read_text(encoding="utf-8") == "one\ntwo\n"


def test_secure_create_returns_owner_only_fd(tmp_path) -> None:
    path = tmp_path / "nested" / "file.txt"
    fd = secure_create(path)
    os.close(fd)

    if _EXACT_MODE_BITS_SUPPORTED:
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.exists()


def test_secure_open_append_refuses_symlink(tmp_path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        # Creating a symlink itself needs Developer Mode/admin on Windows;
        # skip there rather than fail on an environment limitation unrelated
        # to what this test actually verifies.
        pytest.skip(f"cannot create symlinks in this environment: {exc}")

    with pytest.raises(OSError):
        with secure_open_append(link):
            pass


def test_secure_create_succeeds_without_o_nofollow(monkeypatch, tmp_path) -> None:
    """Platforms lacking O_NOFOLLOW (Windows) fall back to opening without it."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    path = tmp_path / "no_nofollow" / "file.txt"

    fd = secure_create(path)
    os.close(fd)

    assert path.exists()


def test_secure_create_succeeds_without_fchmod(monkeypatch, tmp_path) -> None:
    """Platforms lacking fchmod (Windows) skip the post-open chmod instead of raising."""
    monkeypatch.delattr(os, "fchmod", raising=False)
    path = tmp_path / "no_fchmod" / "file.txt"

    fd = secure_create(path)
    os.close(fd)

    assert path.exists()
