#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The files a PAM capture session leaves for a restart, and for after it ends.

Each kept session is one JSON file, owner-only, replaced whole by a rename so a
crash never leaves half of one; a session that ends becomes a previous one,
kept for its days and within its owner's cap. Reading the files back is
``test_capture_record.py``'s.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.server import capture_record as cr
from provide.uterm.server import capture_state as cs
from provide.uterm.server.capture_state import (
    FORMAT_VERSION,
    PREVIOUS_PER_OWNER,
    SCREEN_SAVE_INTERVAL_S,
    CaptureRecord,
    CaptureStateStore,
    ScreenKeeper,
)

from .capture_state_helpers import (
    DAY,
    SID,
    Clock,
    definition,
    live_path,
    mode,
    previous_path,
    private_dir,
    snapshot,
    store,
)

# ── writing ──────────────────────────────────────────────────────────────────


async def test_a_definition_is_written_owner_only_in_owner_only_directories(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.track(definition(), "inst-1")
    await s.save_definition(SID)
    path = live_path(tmp_path)
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700
    assert mode(path.parent.parent) == 0o700
    data = json.loads(path.read_text())
    assert data == {
        "format": FORMAT_VERSION,
        "kind": "live",
        "session": definition().model_dump(mode="json") | {"created_at": data["session"]["created_at"]},
        "recording_instance": "inst-1",
        "login": {"pid": 4242, "start_ticks": 777},
        "screen": None,
        "scrollback": [],
        "saved_at": 0.0,
    }


async def test_a_directory_left_with_a_wider_mode_is_refused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    private_dir(tmp_path / "state" / "live").chmod(0o755)
    s = store(tmp_path)
    s.track(definition(), "i")
    with caplog.at_level(logging.WARNING):
        await s.save_definition(SID)
    assert not live_path(tmp_path).exists()
    assert "live: mode 0755, not 0700" in caplog.text


async def test_a_screen_is_written_with_its_size_and_the_newest_scrollback(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, lines=2, clock=clock)
    s.track(definition(), "i")
    await s.save_screen(SID, snapshot("a\nb", cols=100, rows=30), ["r1", "r2", "r3"])
    data = json.loads(live_path(tmp_path).read_text())
    assert data["screen"] == {"ansi": "a\nb", "cols": 100, "rows": 30}
    assert data["scrollback"] == ["r2", "r3"]
    assert data["saved_at"] == clock.now


async def test_no_scrollback_is_kept_when_previous_sessions_are_off(tmp_path: Path) -> None:
    s = store(tmp_path, keep_days=0, lines=50)
    assert s.scrollback_lines == 0
    s.track(definition(), "i")
    await s.save_screen(SID, snapshot(), ["r1"])
    assert json.loads(live_path(tmp_path).read_text())["scrollback"] == []


async def test_nothing_is_written_without_persist_capture(tmp_path: Path) -> None:
    s = store(tmp_path, persist=False)
    s.track(definition(), "i")
    await s.save_definition(SID)
    await s.save_screen(SID, snapshot(), [])
    assert not (tmp_path / "state").exists()


async def test_saving_an_untracked_session_writes_nothing(tmp_path: Path) -> None:
    s = store(tmp_path)
    await s.save_definition(SID)
    await s.save_screen(SID, snapshot(), [])
    assert not (tmp_path / "state").exists()


async def test_a_failed_write_leaves_the_old_file_whole_and_no_temporary_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    s = store(tmp_path)
    s.track(definition(), "i")
    await s.save_screen(SID, snapshot("old"), [])
    before = live_path(tmp_path).read_bytes()
    with patch.object(Path, "replace", side_effect=OSError("disk full")), caplog.at_level(logging.WARNING):
        await s.save_screen(SID, snapshot("new"), [])
    assert live_path(tmp_path).read_bytes() == before
    assert sorted(p.name for p in live_path(tmp_path).parent.iterdir()) == [f"{SID}.json"]
    assert "capture_state_write_failed" in caplog.text


async def test_the_write_is_synced_before_it_is_renamed_into_place(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.track(definition(), "i")
    order: list[str] = []
    real_fsync, real_replace = os.fsync, Path.replace

    def fsync(fd: int) -> None:
        order.append("fsync")
        real_fsync(fd)

    def replace(self: Path, target: Path) -> Path:
        order.append("rename")
        return real_replace(self, target)

    with patch.object(cr.os, "fsync", fsync), patch.object(Path, "replace", replace):
        await s.save_definition(SID)
    # The file, then the rename, then the directory that holds the rename.
    assert order == ["fsync", "rename", "fsync"]


async def test_a_directory_another_user_owns_is_refused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    s = store(tmp_path)
    s.track(definition(), "i")
    private_dir(tmp_path / "state")
    with patch.object(cr.os, "geteuid", return_value=os.geteuid() + 1), caplog.at_level(logging.WARNING):
        await s.save_definition(SID)
    assert not live_path(tmp_path).exists()
    assert "state: not a directory this user owns" in caplog.text


async def test_scrollback_that_would_outgrow_the_limit_is_dropped_before_the_screen(tmp_path: Path) -> None:
    s = store(tmp_path, lines=10)
    s.track(definition(), "i")
    with patch.object(cs, "MAX_RECORD_BYTES", 2000):
        await s.save_screen(SID, snapshot("kept"), ["x" * 300] * 10)
    data = json.loads(live_path(tmp_path).read_text())
    assert data["screen"]["ansi"] == "kept"
    assert data["scrollback"] == []


async def test_a_screen_too_large_to_read_back_is_not_written(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    s = store(tmp_path)
    s.track(definition(), "i")
    with patch.object(cs, "MAX_RECORD_BYTES", 1000), caplog.at_level(logging.WARNING):
        await s.save_screen(SID, snapshot("y" * 2000), [])
    assert not live_path(tmp_path).exists()
    assert "is too large" in caplog.text


# ── ending ───────────────────────────────────────────────────────────────────


async def test_an_ended_session_becomes_a_previous_session(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock=clock)
    s.track(definition(), "i")
    await s.save_screen(SID, snapshot("last"), ["above"])
    clock.now += 60
    await s.end(SID)
    assert not live_path(tmp_path).exists()
    data = json.loads(previous_path(tmp_path).read_text())
    assert (data["kind"], data["ended_at"], data["screen"]["ansi"], data["scrollback"]) == (
        "previous",
        clock.now,
        "last",
        ["above"],
    )
    assert mode(previous_path(tmp_path)) == 0o600
    assert [r.summary() for r in s.previous_sessions()] == [
        {
            "id": SID,
            "display_name": "suokki (pts/3) [live]",
            "owner": "alice",
            "ended_at": clock.now,
            "cols": 80,
            "rows": 24,
        }
    ]
    # The listing holds no screen; a read takes it from the file.
    assert s.previous_sessions()[0].screen is None
    read = await s.previous_session(SID)
    assert read is not None and (read.screen, read.scrollback) == ("last", ["above"])


async def test_an_ended_session_is_deleted_when_previous_sessions_are_off(tmp_path: Path) -> None:
    s = store(tmp_path, keep_days=0)
    s.track(definition(), "i")
    await s.save_screen(SID, snapshot(), [])
    await s.end(SID)
    assert not live_path(tmp_path).exists()
    assert not previous_path(tmp_path).exists()
    assert s.previous_sessions() == []


async def test_a_session_that_never_showed_a_screen_leaves_no_previous_session(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.track(definition(), "i")
    await s.save_definition(SID)
    await s.end(SID)
    assert not live_path(tmp_path).exists()
    assert not previous_path(tmp_path).exists()


async def test_a_previous_session_that_cannot_be_written_is_not_listed(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.track(definition(), "i")
    await s.save_screen(SID, snapshot(), [])
    with patch.object(cs, "write_file", side_effect=OSError("disk full")):
        await s.end(SID)
    assert s.previous_sessions() == []


async def test_ending_an_untracked_session_does_nothing(tmp_path: Path) -> None:
    s = store(tmp_path)
    await s.end(SID)
    assert not (tmp_path / "state").exists()


async def test_previous_sessions_list_newest_first(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock=clock)
    for pid in (1, 2, 3):
        sid = f"pam-u-capture-{pid}-9"
        s.track(definition(sid), "i")
        await s.save_screen(sid, snapshot(), [])
        clock.now += 1
        await s.end(sid)
    assert [r.session_id for r in s.previous_sessions()] == [f"pam-u-capture-{pid}-9" for pid in (3, 2, 1)]


async def test_an_owner_keeps_only_the_newest_previous_sessions(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock=clock)
    for pid in range(PREVIOUS_PER_OWNER + 2):
        sid = f"pam-u-capture-{pid}-9"
        s.track(definition(sid), "i")
        await s.save_screen(sid, snapshot(), [])
        clock.now += 1
        await s.end(sid)
    # Another owner's are not counted against alice's.
    s.track(definition("pam-b-capture-1-9", owner="bob"), "i")
    await s.save_screen("pam-b-capture-1-9", snapshot(), [])
    await s.end("pam-b-capture-1-9")
    alice = [r.session_id for r in s.previous_sessions() if r.definition.owner == "alice"]
    assert alice == [f"pam-u-capture-{pid}-9" for pid in range(PREVIOUS_PER_OWNER + 1, 1, -1)]
    assert not previous_path(tmp_path, "pam-u-capture-0-9").exists()
    assert not previous_path(tmp_path, "pam-u-capture-1-9").exists()
    assert previous_path(tmp_path, "pam-u-capture-2-9").exists()
    assert previous_path(tmp_path, "pam-b-capture-1-9").exists()


async def test_sessions_with_no_owner_count_against_the_pam_user_their_id_names(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock=clock)
    for pid in range(PREVIOUS_PER_OWNER + 1):
        sid = f"pam-ann-capture-{pid}-9"
        s.track(definition(sid, owner=None), "i")
        await s.save_screen(sid, snapshot(), [])
        clock.now += 1
        await s.end(sid)
    # Ben's one login is not evicted by Ann's many.
    s.track(definition("pam-ben-capture-1-9", owner=None), "i")
    await s.save_screen("pam-ben-capture-1-9", snapshot(), [])
    clock.now += 1
    await s.end("pam-ben-capture-1-9")
    kept = [r.session_id for r in s.previous_sessions()]
    assert len(kept) == PREVIOUS_PER_OWNER + 1
    assert "pam-ben-capture-1-9" in kept
    assert "pam-ann-capture-0-9" not in kept


async def test_previous_sessions_that_ended_at_once_are_ordered_by_id(tmp_path: Path) -> None:
    s = store(tmp_path, clock=Clock())
    for sid in ("pam-u-capture-2-9", "pam-u-capture-1-9", "pam-u-capture-3-9"):
        s.track(definition(sid), "i")
        await s.save_screen(sid, snapshot(), [])
        await s.end(sid)
    assert [r.session_id for r in s.previous_sessions()] == [
        "pam-u-capture-1-9",
        "pam-u-capture-2-9",
        "pam-u-capture-3-9",
    ]


async def test_a_login_that_ended_before_its_days_began_leaves_no_previous_session(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, keep_days=2, clock=clock)
    old = CaptureRecord(definition=definition(), recording_instance="i", screen="x", cols=80, rows=24)
    old.saved_at = clock.now - 2 * DAY - 1
    await s.end_record(old)
    assert s.previous_sessions() == []
    assert not previous_path(tmp_path).exists()
    old.saved_at = clock.now - 2 * DAY
    await s.end_record(old)
    assert [r.session_id for r in s.previous_sessions()] == [SID]


async def test_previous_sessions_past_their_days_are_pruned(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, keep_days=2, clock=clock)
    for pid in (1, 2):
        sid = f"pam-u-capture-{pid}-9"
        s.track(definition(sid), "i")
        await s.save_screen(sid, snapshot(), [])
        await s.end(sid)
        clock.now += DAY
    s.prune()
    # The first ended two days ago, which is still within its days.
    assert len(s.previous_sessions()) == 2
    clock.now += 1
    s.prune()
    assert [r.session_id for r in s.previous_sessions()] == ["pam-u-capture-2-9"]
    assert not previous_path(tmp_path, "pam-u-capture-1-9").exists()
    assert previous_path(tmp_path, "pam-u-capture-2-9").exists()


async def test_previous_sessions_are_read_back_and_pruned_at_startup(tmp_path: Path) -> None:
    clock = Clock()
    writer = store(tmp_path, keep_days=7, clock=clock)
    for pid in (1, 2):
        sid = f"pam-u-capture-{pid}-9"
        writer.track(definition(sid), "i")
        await writer.save_screen(sid, snapshot(f"screen {pid}"), [])
        await writer.end(sid)
        clock.now += 6 * DAY
    reader = store(tmp_path, keep_days=7, clock=clock)
    reader.load_previous()
    assert [r.session_id for r in reader.previous_sessions()] == ["pam-u-capture-2-9"]
    record = await reader.previous_session("pam-u-capture-2-9")
    assert record is not None and record.screen == "screen 2"


async def test_a_previous_session_whose_file_has_gone_reads_as_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    s = store(tmp_path)
    assert await s.previous_session(SID) is None
    s._previous[SID] = CaptureRecord(definition=definition(), recording_instance="i", ended_at=1.0)
    with caplog.at_level(logging.WARNING):
        assert await s.previous_session(SID) is None
    assert "capture_state_file_ignored" in caplog.text
    assert "error=cannot open: ENOENT" in caplog.text


async def test_a_previous_session_that_cannot_be_read_reads_as_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A failing disk answers this read 404, as a vanished file does.
    s = store(tmp_path)
    s.track(definition(), "i")
    await s.save_screen(SID, {"screen": "x", "cols": 80, "rows": 24}, [])
    await s.end(SID)
    with (
        patch.object(cr.os, "fdopen", side_effect=OSError(errno.EIO, "Input/output error")),
        caplog.at_level(logging.WARNING),
    ):
        assert await s.previous_session(SID) is None
    assert "error=cannot read: EIO" in caplog.text


async def test_a_previous_session_is_read_off_the_event_loop(tmp_path: Path) -> None:
    # The file can be megabytes, and the loop serves every terminal.
    s = store(tmp_path)
    s.track(definition(), "i")
    await s.save_screen(SID, {"screen": "x", "cols": 80, "rows": 24}, [])
    await s.end(SID)
    loop_thread = threading.get_ident()
    read_on: list[int] = []
    real = cs._parse_file

    def parse(*args: Any) -> Any:
        read_on.append(threading.get_ident())
        return real(*args)

    with patch.object(cs, "_parse_file", parse):
        record = await s.previous_session(SID)
    assert record is not None
    assert record.screen == "x"
    assert read_on
    assert loop_thread not in read_on


# ── the screen keeper ────────────────────────────────────────────────────────


def _connector(screens: list[str], scrollback: list[str] | None = None) -> MagicMock:
    connector = MagicMock()
    connector.get_snapshot = AsyncMock(side_effect=[snapshot(screen) for screen in screens])
    connector.scrollback = MagicMock(return_value=scrollback or [])
    return connector


def _keeper(tmp_path: Path, **kw: Any) -> tuple[CaptureStateStore, ScreenKeeper]:
    s = store(tmp_path, **kw)
    keeper = s.keeper(definition())
    assert keeper is not None
    return s, keeper


def _saved(tmp_path: Path) -> str | None:
    path = live_path(tmp_path)
    return json.loads(path.read_text())["screen"]["ansi"] if path.exists() else None


async def test_a_screen_is_saved_once_output_settles(tmp_path: Path) -> None:
    _s, keeper = _keeper(tmp_path)
    connector = _connector(["painted"])
    keeper.note_output(100.0)
    await keeper.save_if_due(connector, 100.1)
    assert _saved(tmp_path) is None
    await keeper.save_if_due(connector, 100.25)
    assert _saved(tmp_path) == "painted"


async def test_output_that_never_pauses_is_saved_once_an_interval(tmp_path: Path) -> None:
    _s, keeper = _keeper(tmp_path)
    connector = _connector(["busy"])
    for tick in range(10):
        keeper.note_output(100.0 + tick / 10)
    await keeper.save_if_due(connector, 100.0 + SCREEN_SAVE_INTERVAL_S - 0.05)
    assert _saved(tmp_path) is None
    await keeper.save_if_due(connector, 100.0 + SCREEN_SAVE_INTERVAL_S)
    assert _saved(tmp_path) == "busy"


async def test_no_screen_is_saved_without_new_output(tmp_path: Path) -> None:
    _s, keeper = _keeper(tmp_path)
    connector = _connector([])
    await keeper.save_if_due(connector, 100.0)
    connector.get_snapshot.assert_not_awaited()


async def test_screens_are_saved_no_closer_than_the_interval(tmp_path: Path) -> None:
    _s, keeper = _keeper(tmp_path)
    connector = _connector(["one", "two"])
    keeper.note_output(100.0)
    await keeper.save_if_due(connector, 100.5)
    keeper.note_output(100.6)
    await keeper.save_if_due(connector, 100.0 + SCREEN_SAVE_INTERVAL_S)
    assert _saved(tmp_path) == "one"
    await keeper.save_if_due(connector, 100.5 + SCREEN_SAVE_INTERVAL_S)
    assert _saved(tmp_path) == "two"


async def test_an_unchanged_screen_is_not_written_again(tmp_path: Path) -> None:
    _s, keeper = _keeper(tmp_path)
    connector = _connector(["same", "same"])
    keeper.note_output(100.0)
    await keeper.save_if_due(connector, 101.0)
    mtime = live_path(tmp_path).stat().st_mtime_ns
    keeper.note_output(102.0)
    with patch.object(cs, "write_file") as write:
        await keeper.save_if_due(connector, 103.0)
    write.assert_not_called()
    assert live_path(tmp_path).stat().st_mtime_ns == mtime


async def test_a_screen_is_saved_on_stop_even_unchanged(tmp_path: Path) -> None:
    s, keeper = _keeper(tmp_path)
    connector = _connector(["same", "same"], scrollback=["up"])
    keeper.note_output(100.0)
    await keeper.save_if_due(connector, 101.0)
    with patch.object(cs, "write_file", wraps=cs.write_file) as write:
        await keeper.save_now(connector)
    write.assert_called_once()
    assert json.loads(live_path(tmp_path).read_text())["scrollback"] == ["up"]


async def test_a_screen_that_cannot_be_taken_is_logged_by_type_and_tried_an_interval_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _s, keeper = _keeper(tmp_path)
    connector = MagicMock()
    connector.get_snapshot = AsyncMock(side_effect=RuntimeError("token=hunter2"))
    keeper.note_output(100.0)
    with caplog.at_level(logging.WARNING):
        await keeper.save_if_due(connector, 101.0)
        await keeper.save_now(connector)
    assert "capture_state_screen_failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "hunter2" not in caplog.text
    assert _saved(tmp_path) is None


def test_a_fresh_connector_is_given_the_scrollback_bound(tmp_path: Path) -> None:
    _s, keeper = _keeper(tmp_path, lines=42)
    connector = MagicMock()
    keeper.attach(connector)
    connector.keep_scrollback.assert_called_once_with(42, [])
    connector.restore_screen.assert_not_called()


async def test_a_connector_started_again_carries_on_the_scrollback_kept_so_far(tmp_path: Path) -> None:
    _s, keeper = _keeper(tmp_path)
    await keeper.save_now(_connector(["first"], scrollback=["scrolled before the stop"]))
    again = MagicMock()
    keeper.attach(again)
    again.keep_scrollback.assert_called_once_with(1000, ["scrolled before the stop"])
    again.restore_screen.assert_not_called()


async def test_a_restored_record_is_painted_into_the_first_connector_only(tmp_path: Path) -> None:
    s = store(tmp_path)
    record = CaptureRecord(
        definition=definition(), recording_instance="kept", screen="back", cols=90, rows=30, scrollback=["s"]
    )
    s.adopt(record)
    keeper = s.keeper(record.definition)
    assert keeper is not None and keeper.recording_instance == "kept"
    first, second = MagicMock(), MagicMock()
    keeper.attach(first)
    first.keep_scrollback.assert_called_once_with(1000, ["s"])
    first.restore_screen.assert_called_once_with("back", 90, 30)
    keeper.attach(second)
    second.restore_screen.assert_not_called()
    # The restored screen counts as saved: painting it back changes nothing.
    keeper.note_output(100.0)
    with patch.object(cs, "write_file") as write:
        await keeper.save_if_due(_connector(["back"]), 101.0)
    write.assert_not_called()


def test_a_restored_record_without_a_screen_paints_nothing(tmp_path: Path) -> None:
    s = store(tmp_path)
    record = CaptureRecord(definition=definition(), recording_instance="kept")
    s.adopt(record)
    keeper = s.keeper(record.definition)
    assert keeper is not None
    connector = MagicMock()
    keeper.attach(connector)
    connector.restore_screen.assert_not_called()


async def test_a_kept_record_is_never_replaced_until_its_session_ends(tmp_path: Path) -> None:
    s = store(tmp_path)
    first = s.track(definition(), "one")
    assert s.track(definition(), "two") is first
    await s.end(SID)
    second = s.track(definition(), "two")
    assert second is not first and second.recording_instance == "two"


async def test_a_session_that_is_not_a_pam_capture_cannot_be_written(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    s = store(tmp_path)
    s.track(definition("my-capture"), "i")
    with caplog.at_level(logging.WARNING):
        await s.save_definition("my-capture")
    assert "capture_state_write_failed session_id=my-capture error=not a PAM capture session: my-capture" in caplog.text
