#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A recording never carries one session instance's entries into another's.

A session id can be reused: a PAM capture session is named after a pid, and the
kernel reuses pids. A new session under an old id must not be handed the old
session's recording, so ``start_session`` begins a fresh recording unless the
recording on file was started by the same instance (a reconnect).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from provide.uterm import recording as recording_module
from provide.uterm.recording import (
    InMemoryRecordingStore,
    LocalFileRecordingStore,
    NullRecordingStore,
    RecordingStore,
    recording_instance,
    retire_recording,
    retired_recording_name,
)


def _stores(tmp_path: Path) -> list[RecordingStore]:
    return [LocalFileRecordingStore(tmp_path), InMemoryRecordingStore()]


async def _record(store: RecordingStore, session_id: str, metadata: dict[str, object], text: str) -> None:
    await store.start_session(session_id, metadata)
    await store.append_events(session_id, [{"ts": 1.0, "event": "read", "data": {"text": text}}])
    await store.end_session(session_id)


def _texts(entries: list[dict[str, object]]) -> list[object]:
    return [e["data"]["text"] for e in entries if e["event"] == "read"]  # type: ignore[index]


@pytest.mark.parametrize("kind", [0, 1], ids=["file", "memory"])
async def test_a_new_instance_under_a_reused_id_sees_none_of_the_old_entries(tmp_path: Path, kind: int) -> None:
    store = _stores(tmp_path)[kind]
    await _record(store, "pam-u-capture-7", {"instance": "first"}, "alice-secret")
    await _record(store, "pam-u-capture-7", {"instance": "second"}, "bob-output")

    entries = await store.get_entries("pam-u-capture-7", limit=500)
    assert _texts(entries) == ["bob-output"]
    assert [e["event"] for e in entries] == ["log_start", "read", "log_stop"]
    meta = await store.recording_meta("pam-u-capture-7")
    assert meta["size_bytes"] == sum(len(json.dumps(e)) + 1 for e in entries)


@pytest.mark.parametrize("kind", [0, 1], ids=["file", "memory"])
async def test_a_start_without_an_instance_always_begins_afresh(tmp_path: Path, kind: int) -> None:
    store = _stores(tmp_path)[kind]
    await _record(store, "s", {}, "one")
    await _record(store, "s", {}, "two")
    assert _texts(await store.get_entries("s", limit=500)) == ["two"]


@pytest.mark.parametrize("kind", [0, 1], ids=["file", "memory"])
async def test_the_same_instance_reconnecting_keeps_one_recording(tmp_path: Path, kind: int) -> None:
    store = _stores(tmp_path)[kind]
    await _record(store, "s", {"instance": "i1"}, "first-connection")
    await _record(store, "s", {"instance": "i1"}, "second-connection")
    entries = await store.get_entries("s", limit=500)
    assert _texts(entries) == ["first-connection", "second-connection"]
    assert [e["event"] for e in entries].count("log_start") == 2


async def test_the_old_file_is_kept_aside_for_retention_not_served(tmp_path: Path) -> None:
    store = LocalFileRecordingStore(tmp_path)
    await _record(store, "s", {"instance": "first"}, "old")
    await _record(store, "s", {"instance": "second"}, "new")

    current = tmp_path / "s.jsonl"
    retired = sorted(p for p in tmp_path.glob("s.*.jsonl"))
    assert len(retired) == 1
    # A retired name carries a "." no session id may contain, so no session's
    # recording path can ever resolve to it.
    assert "." in retired[0].name.removesuffix(".jsonl")
    assert "old" in retired[0].read_text()
    assert "old" not in current.read_text()
    assert retired[0].stat().st_mode & 0o777 == 0o600
    assert await store.get_path("s") == current


async def test_an_unreadable_recording_is_retired_rather_than_appended_to(tmp_path: Path) -> None:
    (tmp_path / "s.jsonl").write_text("not json\n")
    store = LocalFileRecordingStore(tmp_path)
    await _record(store, "s", {"instance": "i1"}, "mine")
    assert (tmp_path / "s.jsonl").read_text().startswith('{"ts"')
    assert _texts(await store.get_entries("s", limit=500)) == ["mine"]


@pytest.mark.parametrize("first_line", ['{"event": "read", "data": {}}', "[1, 2]"], ids=["not-start", "not-object"])
async def test_a_recording_not_begun_by_a_log_start_is_retired(tmp_path: Path, first_line: str) -> None:
    (tmp_path / "s.jsonl").write_text(first_line + "\n")
    store = LocalFileRecordingStore(tmp_path)
    await _record(store, "s", {"instance": "i1"}, "mine")
    assert _texts(await store.get_entries("s", limit=500)) == ["mine"]
    assert [p.read_text() for p in tmp_path.glob("s.*.jsonl")] == [first_line + "\n"]


async def test_a_symlink_planted_at_the_path_is_retired_unread(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_text(json.dumps({"ts": 0, "event": "log_start", "data": {"instance": "i1"}}) + "\n")
    (tmp_path / "s.jsonl").symlink_to(target)
    store = LocalFileRecordingStore(tmp_path)
    await _record(store, "s", {"instance": "i1"}, "mine")
    assert not (tmp_path / "s.jsonl").is_symlink()
    assert _texts(await store.get_entries("s", limit=500)) == ["mine"]
    assert target.read_text().count("\n") == 1  # never written through


async def test_retire_session_after_the_session_ended(tmp_path: Path) -> None:
    store = LocalFileRecordingStore(tmp_path)
    await _record(store, "s", {"instance": "a"}, "old")
    await store.retire_session("s")  # no handle open
    assert not (tmp_path / "s.jsonl").exists()
    assert len(list(tmp_path.glob("s.*.jsonl"))) == 1


async def test_the_null_store_retires_nothing() -> None:
    store = NullRecordingStore()
    await store.retire_session("s")
    assert (await store.recording_meta("s"))["exists"] is False


async def test_a_handle_left_open_by_an_unended_instance_is_not_reused(tmp_path: Path) -> None:
    store = LocalFileRecordingStore(tmp_path)
    await store.start_session("s", {"instance": "first"})
    await store.append_events("s", [{"ts": 1.0, "event": "read", "data": {"text": "old"}}])
    # No end_session: the first instance went away without closing.
    await _record(store, "s", {"instance": "second"}, "new")
    assert _texts(await store.get_entries("s", limit=500)) == ["new"]


@pytest.mark.parametrize("kind", [0, 1], ids=["file", "memory"])
async def test_retire_session_leaves_nothing_to_serve(tmp_path: Path, kind: int) -> None:
    store = _stores(tmp_path)[kind]
    await store.start_session("s", {"instance": "a"})
    await store.append_events("s", [{"ts": 1.0, "event": "read", "data": {"text": "old"}}])
    await retire_recording(store, "s")  # an open handle too: no end_session first
    assert await store.get_entries("s", limit=500) == []
    assert (await store.recording_meta("s"))["exists"] is False
    # A later write starts a new recording rather than reviving the old one.
    await _record(store, "s", {"instance": "a"}, "new")
    assert _texts(await store.get_entries("s", limit=500)) == ["new"]


async def test_retire_recording_skips_a_store_without_it() -> None:
    class Legacy:
        pass

    await retire_recording(Legacy(), "s")  # no AttributeError


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"instance": "i1"}, "i1"),
        ({"instance": ""}, None),
        ({"instance": 5}, None),
        ({"instance": None}, None),
        ({}, None),
        ("instance", None),
        (None, None),
    ],
)
def test_recording_instance_names_only_a_non_empty_string(metadata: Any, expected: str | None) -> None:
    assert recording_instance(metadata) == expected


def test_a_retired_name_is_the_id_a_dot_a_timestamp_and_eight_hex_digits() -> None:
    assert re.fullmatch(r"s\.[0-9]+-[0-9a-f]{8}\.jsonl", retired_recording_name("s"))


async def test_a_first_line_that_is_not_utf8_is_retired_not_raised(tmp_path: Path) -> None:
    (tmp_path / "s.jsonl").write_bytes(b"\x80\x81 not text\n")
    store = LocalFileRecordingStore(tmp_path)
    await _record(store, "s", {"instance": "i1"}, "mine")
    assert _texts(await store.get_entries("s", limit=500)) == ["mine"]


async def test_a_first_line_past_the_read_limit_is_not_one_this_store_wrote(tmp_path: Path) -> None:
    """Only the first 64 KiB is read: a longer log_start does not parse, and is retired."""
    start = {"ts": 0, "event": "log_start", "data": {"instance": "i1", "pad": "x" * (64 * 1024)}}
    (tmp_path / "s.jsonl").write_text(json.dumps(start) + "\n")
    store = LocalFileRecordingStore(tmp_path)
    await _record(store, "s", {"instance": "i1"}, "mine")
    assert len(list(tmp_path.glob("s.*.jsonl"))) == 1
    assert [e["event"] for e in await store.get_entries("s", limit=500)] == ["log_start", "read", "log_stop"]


def test_the_instance_is_read_with_plain_o_rdonly_where_o_nofollow_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({"ts": 0, "event": "log_start", "data": {"instance": "i1"}}) + "\n")
    flags: list[int] = []
    real_open = os.open

    def spy(p: Any, f: int, *args: Any) -> int:
        flags.append(f)
        return real_open(p, f, *args)

    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(os, "open", spy)
    assert recording_module._instance_on_file(path) == "i1"
    assert flags == [os.O_RDONLY]


async def test_a_restart_closes_the_handle_an_earlier_start_left_open(tmp_path: Path) -> None:
    store = LocalFileRecordingStore(tmp_path)
    await store.start_session("s", {"instance": "first"})
    handle = store._files["s"]
    await store.start_session("s", {"instance": "second"})
    assert handle.closed
    assert store._files["s"] is not handle


async def test_retiring_closes_the_open_handle_and_later_writes_start_a_new_file(tmp_path: Path) -> None:
    store = LocalFileRecordingStore(tmp_path)
    await store.start_session("s", {"instance": "a"})
    handle = store._files["s"]
    await store.retire_session("s")
    assert handle.closed
    await store.append_events("s", [{"ts": 2.0, "event": "read", "data": {"text": "after"}}])
    (retired,) = tmp_path.glob("s.*.jsonl")
    assert "after" not in retired.read_text()
    assert "after" in (tmp_path / "s.jsonl").read_text()


async def test_retire_session_waits_for_the_session_lock(tmp_path: Path) -> None:
    store = LocalFileRecordingStore(tmp_path)
    await _record(store, "s", {"instance": "a"}, "old")
    lock = store._get_lock("s")
    await lock.acquire()
    retiring = asyncio.create_task(store.retire_session("s"))
    await asyncio.sleep(0.01)
    assert not retiring.done()
    assert (tmp_path / "s.jsonl").exists()
    lock.release()
    await retiring
    assert not (tmp_path / "s.jsonl").exists()
