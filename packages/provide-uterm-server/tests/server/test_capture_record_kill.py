#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The record file's edges, each pinned as exactly as the code decides it.

Mutation-enforced (capture_record.py is in [tool.mutmut] source_paths): the
int64 and size boundaries, the id-length limit, the smallest screen, how much
of a file is ever read, where a temporary file is made and with what mode a
directory is, and the errno names every refusal is worded in.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from provide.uterm.server import capture_record as cr
from provide.uterm.server.capture_record import RecordError, login_identity, parse_record

from .capture_state_helpers import SID, private_dir, valid


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        (0, True),
        (-(2**63), True),
        (2**63 - 1, True),
        (2**63, False),
        (-(2**63) - 1, False),
        (True, False),
        (False, False),
        (1.0, False),
        ("1", False),
        (None, False),
    ],
)
def test_an_integer_is_a_json_integer_within_64_bits(value: object, accepted: bool) -> None:
    assert cr._is_int(value) is accepted


def test_an_id_at_the_byte_limit_still_names_a_login() -> None:
    session_id = "pam-" + "u" * 184 + "-capture-1-2"
    assert len(session_id.encode()) == 200
    assert login_identity(session_id) == (1, 2)
    assert login_identity("pam-" + "u" * 185 + "-capture-1-2") is None


def test_start_ticks_at_the_int64_limit_still_name_a_login() -> None:
    assert login_identity(f"pam-u-capture-1-{2**63 - 1}") == (1, 2**63 - 1)


def test_the_smallest_screen_is_one_cell() -> None:
    record = parse_record(valid(screen={"ansi": "s", "cols": 1, "rows": 1}), session_id=SID, kind="live")
    assert (record.cols, record.rows) == (1, 1)


@pytest.mark.parametrize("screen", [{"ansi": "s", "cols": 80, "rows": 0}, {"ansi": "s", "cols": 80, "rows": -1}])
def test_a_screen_with_no_height_is_refused(screen: dict[str, Any]) -> None:
    with pytest.raises(RecordError, match=r"\Abad screen\Z"):
        parse_record(valid(screen=screen), session_id=SID, kind="live")


@pytest.mark.parametrize(
    ("exc", "name"),
    [
        (OSError(errno.ENOENT, "gone"), "ENOENT"),
        (OSError(errno.EPERM, "nope"), "EPERM"),
        (OSError(9999, "odd"), "9999"),
        (OSError("no errno at all"), "no errno at all"),
    ],
)
def test_an_os_error_is_named_by_its_errno_or_its_own_text(exc: OSError, name: str) -> None:
    assert cr.errno_name(exc) == name


# ── reading ──────────────────────────────────────────────────────────────────


def _file(tmp_path: Path, content: bytes) -> Path:
    path = private_dir(tmp_path / "state" / "live") / f"{SID}.json"
    path.write_bytes(content)
    return path


def test_a_file_exactly_at_the_limit_is_read(tmp_path: Path) -> None:
    path = _file(tmp_path, b"x" * 100)
    with patch.object(cr, "MAX_RECORD_BYTES", 100):
        assert cr.read_file(path) == b"x" * 100


def test_a_file_one_byte_past_the_limit_is_refused_unread(tmp_path: Path) -> None:
    path = _file(tmp_path, b"x" * 101)
    with patch.object(cr, "MAX_RECORD_BYTES", 100), pytest.raises(RecordError, match=r"\Atoo large\Z"):
        cr.read_file(path)


def test_no_more_than_one_byte_past_the_limit_is_ever_read(tmp_path: Path) -> None:
    # A file that grows after it was checked is read only as far as proves it too large.
    path = _file(tmp_path, b"x" * 10)
    real_fdopen = os.fdopen
    asked: list[Any] = []

    def fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
        handle = real_fdopen(fd, *args, **kwargs)
        spy = MagicMock(wraps=handle)
        spy.__enter__ = MagicMock(return_value=spy)
        spy.__exit__ = MagicMock(side_effect=lambda *a: handle.__exit__(*a))
        spy.read = MagicMock(side_effect=lambda n=-1: asked.append(n) or handle.read(n))
        return spy

    with patch.object(cr.os, "fdopen", fdopen), patch.object(cr, "MAX_RECORD_BYTES", 100):
        assert cr.read_file(path) == b"x" * 10
    assert asked == [101]


def test_a_file_is_opened_without_following_a_link_or_blocking(tmp_path: Path) -> None:
    path = _file(tmp_path, b"{}")
    real_open = os.open
    flags: list[int] = []

    def opener(target: Any, how: int, *args: Any) -> int:
        flags.append(how)
        return real_open(target, how, *args)

    with patch.object(cr.os, "open", opener):
        cr.read_file(path)
    assert flags == [os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK]


def test_bytes_that_are_not_utf8_are_not_an_object() -> None:
    with pytest.raises(RecordError, match=r"\Anot an object\Z"):
        cr.parse_bytes(b'{"format": "\xff"}', session_id=SID, kind="live")
    record = cr.parse_bytes(
        json.dumps(valid(screen={"ansi": "é", "cols": 1, "rows": 1})).encode(), session_id=SID, kind="live"
    )
    assert record.screen == "é"


# ── writing ──────────────────────────────────────────────────────────────────


def test_a_temporary_file_is_made_beside_its_target(tmp_path: Path) -> None:
    target = private_dir(tmp_path / "state" / "live") / f"{SID}.json"
    with patch.object(cr.tempfile, "mkstemp", wraps=cr.tempfile.mkstemp) as mkstemp:
        cr.write_file(target, b"{}")
    mkstemp.assert_called_once_with(dir=target.parent, prefix=cr.TMP_PREFIX)
    assert target.read_bytes() == b"{}"


def test_a_directory_that_cannot_be_synced_is_left_as_it_is(tmp_path: Path) -> None:
    target = private_dir(tmp_path / "state" / "live") / f"{SID}.json"
    real_fsync = os.fsync
    calls = {"n": 0}

    def fsync(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # the directory's, after the file's own
            raise OSError(errno.EINVAL, "not supported")
        real_fsync(fd)

    with patch.object(cr.os, "fsync", fsync):
        cr.write_file(target, b"{}")
    assert target.read_bytes() == b"{}"


def test_a_directory_is_made_owner_only_from_the_start(tmp_path: Path) -> None:
    # Made 0700 by mkdir itself, not only by the chmod after it: no window
    # where it is wider.
    target = tmp_path / "state"
    with patch.object(Path, "mkdir", autospec=True, side_effect=Path.mkdir) as mkdir:
        cr.make_private_directory(target)
    mkdir.assert_called_once_with(target, mode=0o700)
