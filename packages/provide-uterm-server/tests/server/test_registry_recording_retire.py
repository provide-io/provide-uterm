#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A deleted session's recording is never served to the id's next holder.

Recording reads are authorized against whichever session holds the id now, so
a session created under a reused id must not see the earlier holder's entries:
not before it starts recording, and not if it never records at all. The
registry retires the recording when a session is deleted (and any leftover when
one is created), and keeps the id reserved until the old runtime has stopped
and the recording is retired, so the old runtime's last writes cannot land in
the new holder's recording either.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from provide.uterm.recording import InMemoryRecordingStore, LocalFileRecordingStore, RecordingStore
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.config_schema import RecordingConfig
from provide.uterm.server.registry import SessionRegistry, SessionValidationError

SID = "pam-suokki-capture-4242-777"


def _registry(store: RecordingStore, directory: Path) -> SessionRegistry:
    return SessionRegistry(
        [],
        hub=TermHub(),
        public_base_url="http://127.0.0.1:9",
        recording=RecordingConfig(directory=directory),
        recording_store=store,
    )


def _payload(session_id: str = SID) -> dict[str, Any]:
    return {"session_id": session_id, "connector_type": "shell", "auto_start": False, "owner": "x"}


async def _record(store: RecordingStore, text: str, instance: str = "a") -> None:
    await store.start_session(SID, {"instance": instance})
    await store.append_events(SID, [{"ts": 1.0, "event": "read", "data": {"text": text}}])
    await store.end_session(SID)


def _stores(tmp_path: Path) -> list[RecordingStore]:
    return [LocalFileRecordingStore(tmp_path), InMemoryRecordingStore()]


@pytest.mark.parametrize("kind", [0, 1], ids=["file", "memory"])
async def test_a_recreated_id_reads_nothing_of_the_deleted_session(tmp_path: Path, kind: int) -> None:
    store = _stores(tmp_path)[kind]
    registry = _registry(store, tmp_path)
    await registry.create_session(_payload())
    await _record(store, "secretA")
    assert await registry.recording_entries(SID)  # A can read its own recording

    await registry.delete_session(SID)
    await registry.create_session(_payload())

    # Before the new session has started -- and it may never start.
    assert await registry.recording_entries(SID, limit=500) == []
    assert (await registry.recording_meta(SID))["exists"] is False
    assert await registry.recording_path(SID) is None


@pytest.mark.parametrize("kind", [0, 1], ids=["file", "memory"])
async def test_a_leftover_recording_is_retired_when_its_id_is_created(tmp_path: Path, kind: int) -> None:
    """A recording with no session (from before a restart) is not the new session's."""
    store = _stores(tmp_path)[kind]
    await _record(store, "secret-from-before-restart")
    registry = _registry(store, tmp_path)
    await registry.create_session(_payload())
    assert await registry.recording_entries(SID, limit=500) == []


async def test_the_deleted_file_is_kept_aside_for_retention(tmp_path: Path) -> None:
    store = LocalFileRecordingStore(tmp_path)
    registry = _registry(store, tmp_path)
    await registry.create_session(_payload())
    await _record(store, "secretA")
    await registry.delete_session(SID)
    assert not (tmp_path / f"{SID}.jsonl").exists()
    retired = list(tmp_path.glob(f"{SID}.*.jsonl"))
    assert len(retired) == 1
    assert "secretA" in retired[0].read_text()


async def test_the_old_runtimes_last_writes_land_before_the_id_is_released(tmp_path: Path) -> None:
    """Delete waits for the old runtime's final flush, then retires; a create meanwhile is refused."""
    store = LocalFileRecordingStore(tmp_path)
    registry = _registry(store, tmp_path)
    await registry.create_session(_payload())
    await store.start_session(SID, {"instance": "a"})

    stopping = asyncio.Event()
    release = asyncio.Event()
    runtime = registry.get_runtime(SID)
    assert runtime is not None

    async def slow_stop() -> None:
        stopping.set()
        await release.wait()
        # The old logger's final flush and log_stop, written during stop().
        await store.append_events(SID, [{"ts": 2.0, "event": "read", "data": {"text": "late-A"}}])
        await store.end_session(SID)

    runtime.stop = slow_stop  # type: ignore[method-assign]
    deleting = asyncio.create_task(registry.delete_session(SID))
    await stopping.wait()

    with pytest.raises(ValueError, match="being deleted"):
        await registry.create_session(_payload())

    release.set()
    await deleting
    await registry.create_session(_payload())
    assert await registry.recording_entries(SID, limit=500) == []
    retired = list(tmp_path.glob(f"{SID}.*.jsonl"))
    assert len(retired) == 1
    assert "late-A" in retired[0].read_text()


async def test_a_failing_stop_still_retires_and_releases_the_id(tmp_path: Path) -> None:
    store = InMemoryRecordingStore()
    registry = _registry(store, tmp_path)
    await registry.create_session(_payload())
    await _record(store, "secretA")
    runtime = registry.get_runtime(SID)
    assert runtime is not None

    async def failing_stop() -> None:
        raise RuntimeError("stop failed")

    runtime.stop = failing_stop  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await registry.delete_session(SID)
    await registry.create_session(_payload())
    assert await registry.recording_entries(SID, limit=500) == []


async def test_session_ids_are_capped_so_every_recording_can_be_retired(tmp_path: Path) -> None:
    registry = _registry(InMemoryRecordingStore(), tmp_path)
    await registry.create_session(_payload("a" * 200))
    with pytest.raises(SessionValidationError, match="at most 200 bytes"):
        await registry.create_session(_payload("a" * 201))
    # The cap is in UTF-8 bytes, which is what a filename is measured in.
    with pytest.raises(SessionValidationError, match="at most 200 bytes"):
        await registry.create_session(_payload("端" * 67))


async def test_the_longest_id_can_be_retired_on_disk(tmp_path: Path) -> None:
    store = LocalFileRecordingStore(tmp_path)
    registry = _registry(store, tmp_path)
    sid = "a" * 200
    await registry.create_session(_payload(sid))
    await store.start_session(sid, {"instance": "a"})
    await store.end_session(sid)
    await registry.delete_session(sid)
    assert len(list(tmp_path.glob(f"{sid}.*.jsonl"))) == 1


def test_a_configured_session_id_is_capped_too() -> None:
    from pydantic import ValidationError

    from provide.uterm.server.models import SessionDefinition

    SessionDefinition(session_id="a" * 200)
    with pytest.raises(ValidationError, match="at most 200 bytes"):
        SessionDefinition(session_id="a" * 201)
