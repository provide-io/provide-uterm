#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import os
import stat

import pytest

from provide.uterm.file_io import secure_create, secure_open_append


def test_secure_open_append_creates_owner_only_file_and_parent(tmp_path) -> None:
    path = tmp_path / "logs" / "session.jsonl"

    with secure_open_append(path) as handle:
        handle.write("one\n")

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

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_secure_open_append_refuses_symlink(tmp_path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    with pytest.raises(OSError):
        with secure_open_append(link):
            pass
