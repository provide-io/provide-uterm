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

import json
from pathlib import Path

import pytest

from provide.uterm.recording import (
    InMemoryRecordingStore,
    LocalFileRecordingStore,
    RecordingStore,
    retire_recording,
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
