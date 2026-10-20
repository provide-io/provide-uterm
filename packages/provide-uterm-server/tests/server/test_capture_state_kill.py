#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The capture-state store's edges, each pinned as exactly as the code decides it.

Mutation-enforced (capture_state.py is in [tool.mutmut] source_paths): what is
logged, in its exact words and arguments; the boundaries of the size limit,
the retention cutoff and the per-owner cap; that a session's writes wait on its
own lock and nobody else's; that a file which cannot be removed costs nothing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from provide.uterm.server import capture_state as cs
from provide.uterm.server.capture_state import (
    PREVIOUS_PER_OWNER,
    SCREEN_SAVE_INTERVAL_S,
    CaptureRecord,
    restore_capture_sessions,
)

from .capture_state_helpers import (
    DAY,
    SID,
    Clock,
    definition,
    live_path,
    previous_path,
    private_dir,
    snapshot,
    store,
    valid,
)

# ── what is logged ───────────────────────────────────────────────────────────


def _live_file(tmp_path: Path, name: str, data: Any) -> Path:
    path = private_dir(tmp_path / "state" / "live") / name
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


def test_a_directory_that_will_not_do_is_logged_by_its_path_and_reason(tmp_path: Path) -> None:
    live = private_dir(tmp_path / "state" / "live")
    live.chmod(0o755)
    with patch.object(cs, "logger") as logger:
        assert store(tmp_path).load_live() == []
    logger.warning.assert_called_once_with(
        "capture_state_dir_ignored path=%s error=%s", str(live), "mode 0755, not 0700"
    )


def test_a_file_that_will_not_do_is_logged_by_its_path_and_reason(tmp_path: Path) -> None:
    path = _live_file(tmp_path, f"{SID}.json", "{not json")
    with patch.object(cs, "logger") as logger:
        assert store(tmp_path).load_live() == []
    [(args, _)] = logger.warning.call_args_list
    assert (args[0], args[1], str(args[2])) == (
        "capture_state_file_ignored path=%s error=%s",
        str(path),
        "not an object",
    )


def test_unknown_keys_are_noted_by_the_files_path(tmp_path: Path) -> None:
    data = valid()
    data["session"] |= {"zeta": 1, "alpha": 2}
    path = _live_file(tmp_path, f"{SID}.json", data)
    with patch.object(cs, "logger") as logger:
        [record] = store(tmp_path).load_live()
    assert record.ignored_keys == ("alpha", "zeta")
    logger.info.assert_called_once_with("capture_state_keys_ignored path=%s keys=%s", str(path), "alpha,zeta")
    logger.warning.assert_not_called()


def test_every_file_after_one_that_is_skipped_is_still_read(tmp_path: Path) -> None:
    # Sorted, each of these comes before the good file: none may stop the load.
    _live_file(tmp_path, ".tmp-crash", "half")
    _live_file(tmp_path, "notes.txt", "x")
    _live_file(tmp_path, "pam-a-capture-1-1.json", "{not json")
    _live_file(tmp_path, f"{SID}.json", valid())
    assert [record.session_id for record in store(tmp_path).load_live()] == [SID]
    assert not (tmp_path / "state" / "live" / ".tmp-crash").exists()


def test_a_temporary_file_that_cannot_be_removed_is_left(tmp_path: Path) -> None:
    _live_file(tmp_path, ".tmp-crash", "half")
    _live_file(tmp_path, f"{SID}.json", valid())
    with patch.object(Path, "unlink", side_effect=PermissionError(13, "Permission denied")):
        assert [record.session_id for record in store(tmp_path).load_live()] == [SID]


async def test_a_previous_session_that_cannot_be_read_is_logged_by_its_path(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.track(definition(), "i")
    await s.save_screen(SID, snapshot(), [])
    await s.end(SID)
    previous_path(tmp_path).unlink()
    with patch.object(cs, "logger") as logger:
        assert await s.previous_session(SID) is None
    [(args, _)] = logger.warning.call_args_list
    assert (args[0], args[1], str(args[2])) == (
        "capture_state_file_ignored path=%s error=%s",
        str(previous_path(tmp_path)),
        "cannot open: ENOENT",
    )


async def test_a_failed_write_is_logged_by_its_session(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.track(definition(), "i")
    failure = OSError("disk full")
    with patch.object(cs, "write_file", side_effect=failure), patch.object(cs, "logger") as logger:
        await s.save_screen(SID, snapshot(), [])
    logger.warning.assert_called_once_with("capture_state_write_failed session_id=%s error=%s", SID, failure)


async def test_a_screen_that_cannot_be_taken_is_logged_by_its_session(tmp_path: Path) -> None:
    keeper = store(tmp_path).keeper(definition())
    assert keeper is not None
    connector = MagicMock()
    connector.get_snapshot = AsyncMock(side_effect=RuntimeError("secret text"))
    with patch.object(cs, "logger") as logger:
        await keeper.save_now(connector)
    logger.warning.assert_called_once_with("capture_state_screen_failed session_id=%s error=%s", SID, "RuntimeError")


# ── restoring at startup ─────────────────────────────────────────────────────


async def _restore(tmp_path: Path, data: dict[str, Any], *, allowed: bool, alive: bool, adopted: bool = True) -> Any:
    _live_file(tmp_path, f"{SID}.json", data)
    s = store(tmp_path)
    registry = MagicMock()
    registry.adopt_restored = AsyncMock(return_value=adopted)
    socket_allowed = MagicMock(return_value=allowed)
    with patch.object(cs, "login_is_alive", return_value=alive), patch.object(cs, "logger") as logger:
        await restore_capture_sessions(registry, s, socket_allowed)
    return socket_allowed, registry, logger


async def test_a_socket_outside_the_confinement_is_logged_and_ended(tmp_path: Path) -> None:
    socket_allowed, registry, logger = await _restore(tmp_path, valid(), allowed=False, alive=True)
    socket_allowed.assert_called_once_with("/run/uterm-capture/uterm-cap-4242.sock")
    logger.warning.assert_called_once_with(
        "capture_state_restore_refused session_id=%s reason=socket_not_confined", SID
    )
    registry.adopt_restored.assert_not_called()
    assert not live_path(tmp_path).exists()


async def test_a_definition_with_no_socket_is_asked_about_the_empty_path(tmp_path: Path) -> None:
    data = valid()
    data["session"]["connector_config"] = {}
    socket_allowed, _registry, _logger = await _restore(tmp_path, data, allowed=False, alive=True)
    socket_allowed.assert_called_once_with("")


async def test_a_login_that_is_gone_is_logged_and_ended(tmp_path: Path) -> None:
    _allowed, registry, logger = await _restore(tmp_path, valid(), allowed=True, alive=False)
    logger.info.assert_called_once_with("capture_state_login_gone session_id=%s", SID)
    registry.adopt_restored.assert_not_called()


async def test_a_restored_session_is_logged(tmp_path: Path) -> None:
    _allowed, registry, logger = await _restore(tmp_path, valid(), allowed=True, alive=True)
    registry.adopt_restored.assert_awaited_once()
    logger.info.assert_called_once_with("capture_state_restored session_id=%s", SID)


async def test_a_session_the_registry_will_not_take_is_not_logged_as_restored(tmp_path: Path) -> None:
    _allowed, _registry, logger = await _restore(tmp_path, valid(), allowed=True, alive=True, adopted=False)
    logger.info.assert_not_called()


# ── locks ────────────────────────────────────────────────────────────────────


async def _waits_on(lock: asyncio.Lock, write: Any) -> None:
    """*write* does not finish while *lock* is held, and does once it is released."""
    async with lock:
        task = asyncio.ensure_future(write)
        await asyncio.sleep(0.05)
        assert not task.done()
    await asyncio.wait_for(task, 5)


async def test_a_sessions_writes_wait_on_its_own_lock(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.track(definition(), "i")
    await _waits_on(s._lock(SID), s.save_screen(SID, snapshot(), []))
    await _waits_on(s._lock(SID), s.save_definition(SID))
    await _waits_on(s._lock(SID), s.end(SID))


async def test_one_sessions_lock_holds_up_no_other_session(tmp_path: Path) -> None:
    s = store(tmp_path)
    other = "pam-other-capture-1-2"
    s.track(definition(other), "i")
    async with s._lock(SID):
        await asyncio.wait_for(s.save_screen(other, snapshot(), []), 5)


async def test_an_ended_session_leaves_no_lock_behind(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.track(definition(), "i")
    await s.save_screen(SID, snapshot(), [])
    await s.end(SID)
    assert s._locks == {}


# ── ending ───────────────────────────────────────────────────────────────────


async def test_with_previous_sessions_off_an_ended_session_leaves_no_file(tmp_path: Path) -> None:
    s = store(tmp_path, keep_days=0)
    s.track(definition(), "i")
    await s.save_screen(SID, snapshot(), [])
    await s.end(SID)
    assert not previous_path(tmp_path).exists()
    assert s.previous_sessions() == []


async def test_a_session_with_no_screen_leaves_no_previous_file(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.track(definition(), "i")
    await s.save_definition(SID)
    await s.end(SID)
    assert not previous_path(tmp_path).exists()


async def test_a_live_file_that_cannot_be_removed_costs_nothing(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.track(definition(), "i")
    await s.save_screen(SID, snapshot(), [])
    with patch.object(Path, "unlink", side_effect=PermissionError(13, "Permission denied")):
        await s.end(SID)
    assert [record.session_id for record in s.previous_sessions()] == [SID]


async def test_a_session_whose_live_file_has_gone_still_ends(tmp_path: Path) -> None:
    s = store(tmp_path, persist=False)
    s.track(definition(), "i")
    await s.save_screen(SID, snapshot(), [])
    await s.end(SID)
    assert [record.session_id for record in s.previous_sessions()] == [SID]


def test_one_kept_day_keeps_the_scrollback() -> None:
    assert cs.CaptureStateStore(Path("/x"), persist=True, keep_days=1, scrollback_lines=5).scrollback_lines == 5


# ── the size limit ───────────────────────────────────────────────────────────


async def test_a_record_exactly_at_the_limit_is_written_whole(tmp_path: Path) -> None:
    s = store(tmp_path)
    record = s.track(definition(), "i")
    record.screen, record.cols, record.rows, record.scrollback = "s", 80, 24, ["row"]
    exact = len(cs._encode(record.to_json()))
    with patch.object(cs, "MAX_RECORD_BYTES", exact):
        await s.save_definition(SID)
    assert json.loads(live_path(tmp_path).read_text())["scrollback"] == ["row"]


async def test_a_record_without_scrollback_exactly_at_the_limit_is_written(tmp_path: Path) -> None:
    s = store(tmp_path)
    record = s.track(definition(), "i")
    record.screen, record.cols, record.rows = "s", 80, 24
    exact = len(cs._encode(record.to_json()))
    with patch.object(cs, "MAX_RECORD_BYTES", exact):
        await s.save_definition(SID)
    assert live_path(tmp_path).exists()


async def test_a_file_is_written_compact_as_the_go_port_writes_it(tmp_path: Path) -> None:
    s = store(tmp_path)
    record = s.track(definition(), "i")
    await s.save_definition(SID)
    assert live_path(tmp_path).read_bytes() == json.dumps(record.to_json(), separators=(",", ":")).encode()


# ── retention and the per-owner cap ──────────────────────────────────────────


async def _ended(s: Any, session_id: str, *, owner: str | None = "alice") -> None:
    s.track(definition(session_id, owner=owner), "i")
    await s.save_screen(session_id, snapshot(), [])
    await s.end(session_id)


async def test_an_owner_is_capped_whoever_ended_a_session_last(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock=clock)
    await _ended(s, "pam-b-capture-1-9", owner="bob")
    for pid in range(PREVIOUS_PER_OWNER + 1):
        clock.now += 1
        await _ended(s, f"pam-u-capture-{pid}-9")
    alice = [record for record in s.previous_sessions() if record.definition.owner == "alice"]
    assert len(alice) == PREVIOUS_PER_OWNER
    assert not previous_path(tmp_path, "pam-u-capture-0-9").exists()


async def test_owners_sharing_a_pam_user_are_capped_apart(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock=clock)
    for pid in range(PREVIOUS_PER_OWNER):
        clock.now += 1
        await _ended(s, f"pam-u-capture-{pid}-9", owner="alice")
    await _ended(s, "pam-u-capture-99-9", owner="bob")
    assert len(s.previous_sessions()) == PREVIOUS_PER_OWNER + 1


async def test_an_owner_never_shares_a_cap_with_a_pam_user_of_its_name(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock=clock)
    for pid in range(PREVIOUS_PER_OWNER):
        clock.now += 1
        await _ended(s, f"pam-u-capture-{pid}-9", owner="u")
    await _ended(s, "pam-u-capture-99-9", owner=None)
    assert len(s.previous_sessions()) == PREVIOUS_PER_OWNER + 1


async def test_the_cap_is_applied_to_what_is_read_back(tmp_path: Path) -> None:
    clock = Clock()
    writer = store(tmp_path, clock=clock)
    for pid in range(PREVIOUS_PER_OWNER + 1):
        clock.now += 1
        await _ended(writer, f"pam-u-capture-{pid}-9")
    # The one the cap dropped, put back as a store with no cap would leave it.
    dropped = CaptureRecord(
        definition=definition("pam-u-capture-0-9"),
        recording_instance="i",
        screen="s",
        cols=80,
        rows=24,
        saved_at=clock.now - 100,
        ended_at=clock.now - 100,
    )
    previous_path(tmp_path, "pam-u-capture-0-9").write_text(json.dumps(dropped.to_json()))
    reader = store(tmp_path, clock=clock)
    reader.load_previous()
    kept = [record.session_id for record in reader.previous_sessions()]
    assert len(kept) == PREVIOUS_PER_OWNER
    assert "pam-u-capture-0-9" not in kept
    assert not previous_path(tmp_path, "pam-u-capture-0-9").exists()


async def test_a_session_that_ended_at_the_epoch_is_the_oldest_and_pruned(tmp_path: Path) -> None:
    clock = Clock()
    clock.now = 0.5 + 7 * DAY
    s = store(tmp_path, clock=clock)
    epoch = CaptureRecord(definition=definition("pam-u-capture-1-9"), recording_instance="i", screen="s", ended_at=0.0)
    later = CaptureRecord(definition=definition("pam-u-capture-2-9"), recording_instance="i", screen="s", ended_at=0.5)
    s._previous = {epoch.session_id: epoch, later.session_id: later}
    assert [record.session_id for record in s.previous_sessions()] == ["pam-u-capture-2-9", "pam-u-capture-1-9"]
    s.prune()
    assert [record.session_id for record in s.previous_sessions()] == ["pam-u-capture-2-9"]


async def test_a_previous_file_that_cannot_be_removed_is_still_dropped_from_the_listing(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock=clock)
    await _ended(s, SID)
    clock.now += 8 * DAY
    with patch.object(Path, "unlink", side_effect=PermissionError(13, "Permission denied")):
        s.prune()
    assert s.previous_sessions() == []


# ── the screen keeper ────────────────────────────────────────────────────────


def _connector(*screens: str) -> MagicMock:
    connector = MagicMock()
    connector.get_snapshot = AsyncMock(side_effect=[snapshot(screen) for screen in screens])
    connector.scrollback = MagicMock(return_value=[])
    return connector


async def test_an_empty_first_screen_is_saved(tmp_path: Path) -> None:
    keeper = store(tmp_path).keeper(definition())
    assert keeper is not None
    keeper.note_output(100.0)
    await keeper.save_if_due(_connector(""), 101.0)
    assert json.loads(live_path(tmp_path).read_text())["screen"]["ansi"] == ""


async def test_a_saved_screen_is_not_taken_again_without_new_output(tmp_path: Path) -> None:
    keeper = store(tmp_path).keeper(definition())
    assert keeper is not None
    connector = _connector("one", "two")
    keeper.note_output(100.0)
    await keeper.save_if_due(connector, 101.0)
    await keeper.save_if_due(connector, 101.0 + 10 * SCREEN_SAVE_INTERVAL_S)
    assert connector.get_snapshot.await_count == 1


async def test_a_restored_screen_is_painted_by_the_first_keeper_only(tmp_path: Path) -> None:
    s = store(tmp_path)
    record = CaptureRecord(definition=definition(), recording_instance="kept", screen="back", cols=90, rows=30)
    s.adopt(record)
    first = s.keeper(record.definition)
    second = s.keeper(record.definition)
    assert first is not None and second is not None
    painted, fresh = MagicMock(), MagicMock()
    first.attach(painted)
    second.attach(fresh)
    painted.restore_screen.assert_called_once_with("back", 90, 30)
    fresh.restore_screen.assert_not_called()


def test_attaching_twice_paints_once(tmp_path: Path) -> None:
    s = store(tmp_path)
    record = CaptureRecord(definition=definition(), recording_instance="kept", screen="back", cols=90, rows=30)
    s.adopt(record)
    keeper = s.keeper(record.definition)
    assert keeper is not None
    connector = MagicMock()
    keeper.attach(connector)
    keeper.attach(connector)
    assert connector.restore_screen.call_args_list == [call("back", 90, 30)]


@pytest.mark.parametrize("session_id", [SID])
def test_the_listing_copy_keeps_everything_but_the_screen(session_id: str) -> None:
    record = CaptureRecord(
        definition=definition(session_id),
        recording_instance="inst",
        screen="s",
        cols=80,
        rows=24,
        scrollback=["a"],
        saved_at=5.0,
        ended_at=6.0,
    )
    copy = cs._without_screen(record)
    assert (copy.screen, copy.scrollback) == (None, [])
    assert (copy.recording_instance, copy.cols, copy.rows, copy.saved_at, copy.ended_at) == ("inst", 80, 24, 5.0, 6.0)
