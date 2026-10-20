#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A kept capture session's file is read back only if a server of a format this one reads wrote it.

Judged on the JSON itself, so the Python and Go ports accept and refuse the
same files: the corpus both read (``capture_record_golden.json``) is this
port's own output and refusals, and the files of other builds of the format it
reads. Anything else, and any directory that will not
do, is skipped with a warning, never trusted and never fatal.
"""

from __future__ import annotations

import base64
import errno
import json
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from provide.uterm.server import capture_record as cr
from provide.uterm.server.capture_record import (
    CaptureRecord,
    RecordError,
    login_identity,
    pam_user,
    parse_bytes,
    parse_record,
)

from .capture_state_helpers import SID, definition, private_dir, store, valid

GOLDEN = (
    Path(__file__).resolve().parents[3]
    / "provide-uterm-go"
    / "capturestate"
    / "testdata"
    / "capture_record_golden.json"
)


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


# ── ids ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("session_id", "identity"),
    [
        (SID, (4242, 777)),
        ("pam-a-b-capture-1-2", (1, 2)),
        ("pam-suokki-capture-4242-r1a2b3c4", None),
        ("pam-suokki-pts-3", None),
        ("pam--capture-1-2", None),
        ("xpam-suokki-capture-1-2", None),
        ("pam-suokki-capture-1-2-", None),
        ("pam-sü-capture-1-2", None),
        ("pam-a.b-capture-1-2", None),
        ("provide-shell", None),
        (f"pam-u-capture-{2**63}-1", None),
        (f"pam-u-capture-1-{2**63}", None),
        (f"pam-u-capture-{2**63 - 1}-1", (2**63 - 1, 1)),
        ("pam-" + "u" * 190 + "-capture-1-2", None),
    ],
)
def test_only_a_pam_capture_id_names_a_login(session_id: str, identity: tuple[int, int] | None) -> None:
    assert login_identity(session_id) == identity


def test_the_pam_user_is_read_from_the_id() -> None:
    assert (pam_user(SID), pam_user("pam-a-b-capture-1-2"), pam_user("provide-shell")) == ("suokki", "a-b", None)


def test_only_a_pam_capture_session_has_a_file() -> None:
    record = CaptureRecord(definition=definition("my-capture"), recording_instance="i")
    with pytest.raises(RecordError, match=r"\Anot a PAM capture session: my-capture\Z"):
        record.to_json()


# ── the corpus both ports read ───────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["live", "previous"])
def test_a_file_reads_back_to_the_record_that_writes_it_again(kind: str) -> None:
    data = _golden()["records"][kind]
    record = parse_record(data, session_id=_golden()["session_id"], kind=kind)
    assert record.to_json() == data


def test_every_refusal_in_the_corpus_is_this_ports_own() -> None:
    corpus = _golden()
    assert len(corpus["refusals"]) >= 40
    for case in corpus["refusals"]:
        with pytest.raises(RecordError) as caught:
            parse_record(case["data"], session_id=corpus["session_id"], kind=case["kind"])
        assert str(caught.value) == case["error"], case["name"]


def test_a_file_another_build_of_the_format_wrote_is_read() -> None:
    # An upgrade or a downgrade restarts on the files the other build wrote.
    corpus = _golden()
    assert len(corpus["accepted"]) >= 3
    for case in corpus["accepted"]:
        record = parse_record(case["data"], session_id=corpus["session_id"], kind="live")
        assert record.to_json()["session"] == case["session"], case["name"]
        assert list(record.ignored_keys) == case["ignored_keys"], case["name"]


def test_a_key_this_build_does_not_know_never_reaches_the_connector() -> None:
    data = valid()
    data["session"]["surprise"] = "x"
    record = parse_record(data, session_id=SID, kind="live")
    assert "surprise" not in record.definition.connector_config
    assert record.ignored_keys == ("surprise",)


def test_a_missing_creation_time_is_now() -> None:
    data = valid()
    del data["session"]["created_at"]
    before = datetime.now(UTC)
    assert parse_record(data, session_id=SID, kind="live").definition.created_at >= before


def test_every_file_that_is_not_an_object_is_refused_as_one() -> None:
    corpus = _golden()
    assert corpus["bad_files"]
    for case in corpus["bad_files"]:
        with pytest.raises(RecordError) as caught:
            parse_bytes(base64.b64decode(case["content_b64"]), session_id=corpus["session_id"], kind="live")
        assert str(caught.value) == case["error"] == "not an object", case["name"]


def test_a_record_without_a_screen_reads_back_without_one() -> None:
    record = parse_record(valid(screen=None), session_id=SID, kind="live")
    assert (record.screen, record.cols, record.rows) == (None, 0, 0)


def test_a_connector_type_this_server_has_not_registered_is_a_bad_session() -> None:
    data = valid()
    with (
        patch("provide.uterm.server.connectors.registered_types", return_value=frozenset({"shell"})),
        pytest.raises(RecordError, match=r"\Abad session: connector_type\Z"),
    ):
        parse_record(data, session_id=SID, kind="live")


# ── reading the files ────────────────────────────────────────────────────────


def _write_live(tmp_path: Path, name: str, content: bytes) -> Path:
    path = private_dir(tmp_path / "state" / "live") / name
    path.write_bytes(content)
    return path


def _ignored(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> str:
    with caplog.at_level(logging.WARNING):
        assert store(tmp_path).load_live() == []
    return caplog.text


def test_live_records_are_read_back(tmp_path: Path) -> None:
    _write_live(tmp_path, f"{SID}.json", json.dumps(valid()).encode())
    [record] = store(tmp_path).load_live()
    assert (record.session_id, record.screen) == (SID, "s")


def test_no_directory_reads_as_no_records(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert "capture_state" not in _ignored(tmp_path, caplog)


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (b"{not json", "not an object"),
        (json.dumps(valid(format=0)).encode(), "unknown format"),
        (b"\xc3\x28", "not an object"),
        (b"[" * 20_000, "not an object"),
    ],
)
def test_a_malformed_file_is_skipped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: bytes, error: str
) -> None:
    _write_live(tmp_path, f"{SID}.json", content)
    text = _ignored(tmp_path, caplog)
    assert "capture_state_file_ignored" in text
    assert f"error={error}" in text


def test_unknown_keys_are_noted_once_at_load_and_not_warned(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    data = valid()
    data["session"] |= {"surprise": 1, "another": 2}
    _write_live(tmp_path, f"{SID}.json", json.dumps(data).encode())
    with caplog.at_level(logging.INFO):
        [record] = store(tmp_path).load_live()
    assert record.session_id == SID
    [noted] = [r for r in caplog.records if "capture_state_keys_ignored" in r.getMessage()]
    assert noted.levelno == logging.INFO
    assert "keys=another,surprise " in noted.getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_newer_format_is_skipped_with_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_live(tmp_path, f"{SID}.json", json.dumps(valid(format=2)).encode())
    assert "error=newer format: 2" in _ignored(tmp_path, caplog)


@pytest.mark.parametrize(("call", "error"), [("fstat", "cannot stat: EIO"), ("fdopen", "cannot read: EIO")])
def test_an_io_error_on_one_file_skips_only_that_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, call: str, error: str
) -> None:
    _write_live(tmp_path, f"{SID}.json", json.dumps(valid()).encode())
    with patch.object(cr.os, call, side_effect=OSError(errno.EIO, "Input/output error")):
        assert f"error={error}" in _ignored(tmp_path, caplog)


def test_an_oversized_file_is_skipped_unread(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_live(tmp_path, f"{SID}.json", b" " * (cr.MAX_RECORD_BYTES + 1))
    assert "error=too large" in _ignored(tmp_path, caplog)


def test_a_file_that_grows_while_it_is_read_is_refused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_live(tmp_path, f"{SID}.json", b" " * 101)
    real_fstat = os.fstat

    def fstat(fd: int) -> Any:
        info = real_fstat(fd)
        return MagicMock(st_mode=info.st_mode, st_uid=info.st_uid, st_size=10)

    with patch.object(cr, "MAX_RECORD_BYTES", 100), patch.object(cr.os, "fstat", fstat):
        assert "error=too large" in _ignored(tmp_path, caplog)


def test_another_users_file_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = _write_live(tmp_path, f"{SID}.json", json.dumps(valid()).encode())
    other = path.stat().st_uid + 1
    with patch.object(cr.os, "fstat", lambda fd: MagicMock(st_mode=stat.S_IFREG | 0o600, st_uid=other, st_size=1)):
        assert "error=owned by another user" in _ignored(tmp_path, caplog)


def test_a_symlink_is_not_followed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(valid()))
    link = private_dir(tmp_path / "state" / "live") / f"{SID}.json"
    link.symlink_to(target)
    # By errno name, which every port words the same.
    assert "error=cannot open: ELOOP" in _ignored(tmp_path, caplog)


def test_a_directory_in_place_of_a_file_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (private_dir(tmp_path / "state" / "live") / f"{SID}.json").mkdir()
    assert "error=not a regular file" in _ignored(tmp_path, caplog)


@pytest.mark.timeout(10)
def test_a_fifo_is_refused_without_waiting_for_a_writer(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    os.mkfifo(private_dir(tmp_path / "state" / "live") / f"{SID}.json")
    assert "error=not a regular file" in _ignored(tmp_path, caplog)


def test_a_file_named_for_another_session_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_live(tmp_path, "pam-suokki-capture-1-2.json", json.dumps(valid()).encode())
    assert "error=session id is not the file's name" in _ignored(tmp_path, caplog)


def test_temporary_files_left_by_a_crash_are_removed_and_other_files_ignored(tmp_path: Path) -> None:
    tmp = _write_live(tmp_path, ".tmp-abc", b"half")
    other = _write_live(tmp_path, "notes.txt", b"x")
    assert store(tmp_path).load_live() == []
    assert not tmp.exists()
    assert other.exists()


# ── the directories ──────────────────────────────────────────────────────────


def test_an_unreadable_directory_is_skipped_with_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    live = private_dir(tmp_path / "state" / "live")
    (live / f"{SID}.json").write_text(json.dumps(valid()))
    with patch.object(Path, "iterdir", side_effect=PermissionError(13, "Permission denied")):
        text = _ignored(tmp_path, caplog)
    assert "capture_state_dir_ignored" in text
    assert "error=cannot list: EACCES" in text


@pytest.mark.parametrize(
    ("prepare", "error"),
    [
        (lambda live: live.chmod(0o755), "error=mode 0755, not 0700"),
        (lambda live: (live.rmdir(), live.write_text("x")), "error=not a directory this user owns"),
        (lambda live: (live.rmdir(), live.symlink_to(live.parent)), "error=not a directory this user owns"),
    ],
)
def test_a_directory_that_is_not_ours_alone_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, prepare: Any, error: str
) -> None:
    prepare(private_dir(tmp_path / "state" / "live"))
    text = _ignored(tmp_path, caplog)
    assert "capture_state_dir_ignored" in text
    assert error in text


def test_a_directory_another_user_owns_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    private_dir(tmp_path / "state" / "live")
    with patch.object(cr.os, "geteuid", return_value=os.geteuid() + 1):
        assert "error=not a directory this user owns" in _ignored(tmp_path, caplog)


def test_a_directory_that_cannot_be_examined_says_why() -> None:
    with patch.object(Path, "lstat", side_effect=PermissionError(13, "Permission denied")):
        assert cr.check_private_directory(Path("/x")) == "cannot stat: EACCES"


def test_a_directory_that_cannot_even_be_examined_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with patch.object(Path, "lstat", side_effect=PermissionError(13, "Permission denied")):
        text = _ignored(tmp_path, caplog)
    assert "capture_state_dir_ignored" in text
    assert "error=cannot stat: EACCES" in text
