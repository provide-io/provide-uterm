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
from provide.uterm.server import registry as registry_module
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.config_schema import RecordingConfig
from provide.uterm.server.registry import SessionRegistry, SessionValidationError

SID = "pam-suokki-capture-4242-777"


def _registry(store: RecordingStore, directory: Path, max_sessions: int | None = None) -> SessionRegistry:
    return SessionRegistry(
        [],
        hub=TermHub(),
        public_base_url="http://127.0.0.1:9",
        recording=RecordingConfig(directory=directory),
        recording_store=store,
        max_sessions=max_sessions,
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
    """Delete waits for the old runtime's final flush, then retires; a create meanwhile waits for it.

    This is also a tty-named PAM re-login (``pam-<user>-<tty>``): the next login
    on the tty wants the id while the last one's session is still going.
    """
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

    creating = asyncio.create_task(registry.create_session(_payload()))
    await asyncio.sleep(0.05)
    assert not creating.done()  # waiting on the teardown, not refused

    release.set()
    await deleting
    await creating
    assert await registry.get_definition(SID) is not None
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


async def test_a_create_that_outwaits_the_bound_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "_RESERVED_ID_WAIT_S", 0.05)
    monkeypatch.setattr(registry_module, "_REMOVAL_STOP_TIMEOUT_S", 5.0)
    registry = _registry(InMemoryRecordingStore(), tmp_path)
    await registry.create_session(_payload())
    runtime = registry.get_runtime(SID)
    assert runtime is not None
    release = asyncio.Event()

    async def slow_stop() -> None:
        await release.wait()

    runtime.stop = slow_stop  # type: ignore[method-assign]
    deleting = asyncio.create_task(registry.delete_session(SID))
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="being deleted"):
        await registry.create_session(_payload())
    release.set()
    await deleting


async def test_concurrent_creates_of_one_id_see_already_exists(tmp_path: Path) -> None:
    """The relay retries an open: the loser must get "already exists", which the route treats as idempotent."""

    class SlowRetire(InMemoryRecordingStore):
        async def retire_session(self, session_id: str) -> None:
            await asyncio.sleep(0.02)  # the first create is still retiring when the retries arrive
            await super().retire_session(session_id)

    registry = _registry(SlowRetire(), tmp_path)
    results = await asyncio.gather(*(registry.create_session(_payload()) for _ in range(3)), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(errors) == 2
    assert all(isinstance(e, ValueError) and "already exists" in str(e) for e in errors)


async def test_a_hung_stop_still_retires_and_releases_the_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "_REMOVAL_STOP_TIMEOUT_S", 0.05)
    store = InMemoryRecordingStore()
    registry = _registry(store, tmp_path)
    await registry.create_session(_payload())
    await _record(store, "secretA")
    runtime = registry.get_runtime(SID)
    assert runtime is not None

    async def hung_stop() -> None:
        await asyncio.Event().wait()  # never returns

    runtime.stop = hung_stop  # type: ignore[method-assign]
    await asyncio.wait_for(registry.delete_session(SID), 2.0)
    await registry.create_session(_payload())
    assert await registry.recording_entries(SID, limit=500) == []


async def test_a_create_cancelled_while_waiting_for_the_lock_releases_its_reservation(tmp_path: Path) -> None:
    """Cancelled between its retire and the insert, a create must not leave the id reserved."""
    retiring = asyncio.Event()
    proceed = asyncio.Event()

    class HeldRetire(InMemoryRecordingStore):
        async def retire_session(self, session_id: str) -> None:
            retiring.set()
            await proceed.wait()
            await super().retire_session(session_id)

    registry = _registry(HeldRetire(), tmp_path)
    creating = asyncio.create_task(registry.create_session(_payload()))
    await retiring.wait()
    async with registry._lock:  # contention: the create now has to wait for the lock
        proceed.set()
        await asyncio.sleep(0.01)
        creating.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await creating
    assert SID not in registry._retiring
    await asyncio.wait_for(registry.create_session(_payload()), 1.0)  # not 15 s, not refused


async def test_a_delete_cancelled_mid_retire_releases_its_reservation(tmp_path: Path) -> None:
    retiring = asyncio.Event()

    class HangingRetire(InMemoryRecordingStore):
        async def retire_session(self, session_id: str) -> None:
            if retiring.is_set():
                return
            retiring.set()
            await asyncio.Event().wait()

    store = HangingRetire()
    retiring.set()  # the create's own retire passes straight through
    registry = _registry(store, tmp_path)
    await registry.create_session(_payload())
    retiring.clear()
    deleting = asyncio.create_task(registry.delete_session(SID))
    await retiring.wait()
    deleting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await deleting
    assert SID not in registry._retiring


async def test_a_create_past_its_deadline_is_refused_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry_module, "_RESERVED_ID_WAIT_S", 0.0)
    registry = _registry(InMemoryRecordingStore(), tmp_path)
    await registry.create_session(_payload())
    runtime = registry.get_runtime(SID)
    assert runtime is not None
    release = asyncio.Event()

    async def slow_stop() -> None:
        await release.wait()

    runtime.stop = slow_stop  # type: ignore[method-assign]
    deleting = asyncio.create_task(registry.delete_session(SID))
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="being deleted"):
        await registry.create_session(_payload())
    release.set()
    await deleting


async def test_a_second_delete_during_the_teardown_leaves_it_to_the_first(tmp_path: Path) -> None:
    registry = _registry(InMemoryRecordingStore(), tmp_path)
    await registry.create_session(_payload())
    runtime = registry.get_runtime(SID)
    assert runtime is not None
    release = asyncio.Event()
    stops = 0

    async def slow_stop() -> None:
        nonlocal stops
        stops += 1
        await release.wait()

    runtime.stop = slow_stop  # type: ignore[method-assign]
    deleting = asyncio.create_task(registry.delete_session(SID))
    await asyncio.sleep(0)
    await asyncio.wait_for(registry.delete_session(SID), 1.0)  # returns at once
    assert SID in registry._retiring
    release.set()
    await deleting
    assert stops == 1
    assert SID not in registry._retiring


async def test_a_removal_whose_reservation_is_gone_still_retires(tmp_path: Path) -> None:
    store = InMemoryRecordingStore()
    registry = _registry(store, tmp_path)
    await _record(store, "secretA")
    await registry._finish_removal(SID, None)  # nothing reserved the id
    assert SID not in registry._retiring
    assert (await store.recording_meta(SID))["exists"] is False


async def test_the_session_limit_is_checked_again_at_the_insert(tmp_path: Path) -> None:
    """Two creates of different ids both pass the early limit check; only one fits."""

    class SlowRetire(InMemoryRecordingStore):
        async def retire_session(self, session_id: str) -> None:
            await asyncio.sleep(0.02)
            await super().retire_session(session_id)

    registry = _registry(SlowRetire(), tmp_path, max_sessions=1)
    results = await asyncio.gather(
        registry.create_session(_payload("one")), registry.create_session(_payload("two")), return_exceptions=True
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError) and "session limit reached" in str(errors[0])
    assert not registry._retiring
