#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A PAM capture session outlives a server restart while its login does.

The registry writes a kept session's definition whenever it changes, the
runtime writes its screen as it settles and once more as it stops, and a new
server re-creates every session whose login is still the same process, under
the same id, with the same owner, screen and recording. A login that ended
while the server was down leaves a previous session instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Importing the connector registers pty_capture.
from provide.uterm.pty.capture_connector import CaptureConnector
from provide.uterm.recording import LocalFileRecordingStore
from provide.uterm.server import capture_state as cs
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.capture_state import CaptureStateStore, restore_capture_sessions
from provide.uterm.server.config_schema import RecordingConfig
from provide.uterm.server.models import PamConfig
from provide.uterm.server.pam_integration import capture_socket_allowed
from provide.uterm.server.registry import SessionRegistry
from provide.uterm.server.runtime import HostedSessionRuntime

SID = "pam-suokki-capture-4242-777"
SOCKET_DIR = "/run/blackbetty/uterm-capture"
SOCKET = f"{SOCKET_DIR}/uterm-cap-4242.sock"


def _store(tmp_path: Path, *, persist: bool = True, keep_days: int = 7) -> CaptureStateStore:
    return CaptureStateStore(tmp_path / "state", persist=persist, keep_days=keep_days, scrollback_lines=100)


def _registry(tmp_path: Path, store: CaptureStateStore | None, *, max_sessions: int | None = None) -> SessionRegistry:
    return SessionRegistry(
        [],
        hub=TermHub(),
        public_base_url="http://127.0.0.1:9",
        recording=RecordingConfig(directory=tmp_path / "recordings"),
        recording_store=LocalFileRecordingStore(tmp_path / "recordings"),
        max_sessions=max_sessions,
        capture_state=store,
    )


def _payload(session_id: str = SID, **changes: Any) -> dict[str, Any]:
    # What PAM creates (pam_integration._create_capture_session), not started.
    return {
        "session_id": session_id,
        "display_name": "suokki (pts/3) [live]",
        "connector_type": "pty_capture",
        "connector_config": {"socket_path": SOCKET},
        "input_mode": "open",
        "auto_start": False,
        "ephemeral": False,
        "tags": ["pam", "capture", "suokki"],
        "visibility": "operator",
    } | changes


def _runtime(registry: SessionRegistry) -> HostedSessionRuntime:
    runtime = registry.get_runtime(SID)
    assert runtime is not None
    return runtime


def _live(tmp_path: Path, session_id: str = SID) -> dict[str, Any]:
    return json.loads((tmp_path / "state" / "live" / f"{session_id}.json").read_text())


def _confined(socket_path: str) -> bool:
    return capture_socket_allowed(socket_path, PamConfig(capture_socket_dir=SOCKET_DIR))


def _alive(*pids: int) -> Any:
    """``_process_start_ticks`` for a host where only *pids* run, each started at 777."""
    return patch("provide.uterm.server.pam_integration._process_start_ticks", lambda pid: 777 if pid in pids else None)


async def _painted(text: bytes) -> dict[str, Any]:
    connector = CaptureConnector(SID, "d", {"socket_path": SOCKET, "cols": 30, "rows": 5})
    connector._ingest_stdout(text)
    return connector._snapshot()


# ── the definition, as it changes ────────────────────────────────────────────


async def test_a_created_capture_session_is_written_with_its_definition(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _store(tmp_path))
    await registry.create_session(_payload())
    data = _live(tmp_path)
    assert data["session"]["connector_config"] == {"socket_path": SOCKET}
    assert (data["session"]["owner"], data["session"]["visibility"], data["session"]["input_mode"]) == (
        None,
        "operator",
        "open",
    )
    assert data["recording_instance"] == _runtime(registry)._recording_instance


async def test_the_written_definition_is_the_one_the_owner_patched_in(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _store(tmp_path))
    await registry.create_session(_payload())
    await registry.update_session(
        SID,
        {
            "owner": "blackbetty:7",
            "visibility": "private",
            "tags": ["pam", "capture", "suokki", "blackbetty-input"],
            "connector_config": {"socket_path": SOCKET, "stdin_socket_path": "/run/bb/in.sock", "cols": 100},
        },
        allow_owner_change=True,
    )
    await registry.set_mode(SID, "hijack")
    session = _live(tmp_path)["session"]
    assert (session["owner"], session["visibility"], session["input_mode"]) == ("blackbetty:7", "private", "hijack")
    assert session["tags"][-1] == "blackbetty-input"
    assert session["connector_config"]["stdin_socket_path"] == "/run/bb/in.sock"


async def test_an_update_that_changes_nothing_writes_nothing(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _store(tmp_path))
    await registry.create_session(_payload())
    with patch.object(cs, "write_file") as write:
        await registry.update_session(SID, {"unknown": 1})
    write.assert_not_called()


async def test_other_sessions_are_not_written(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _store(tmp_path))
    await registry.create_session(_payload("not-a-pam-id"))
    await registry.create_session(
        {"session_id": "pam-suokki-capture-1-2", "connector_type": "pty", "auto_start": False}
    )
    await registry.update_session("not-a-pam-id", {"display_name": "x"})
    assert not (tmp_path / "state").exists()


async def test_a_registry_without_a_store_writes_nothing(tmp_path: Path) -> None:
    registry = _registry(tmp_path, None)
    await registry.create_session(_payload())
    await registry.update_session(SID, {"display_name": "x"})
    await registry.delete_session(SID)
    assert not (tmp_path / "state").exists()
    assert registry.get_runtime(SID) is None


# ── the end of a session ─────────────────────────────────────────────────────


async def test_a_deleted_session_leaves_a_previous_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = _registry(tmp_path, store)
    await registry.create_session(_payload())
    await store.save_screen(SID, await _painted(b"$ exit"), [])
    await registry.delete_session(SID)
    assert not (tmp_path / "state" / "live" / f"{SID}.json").exists()
    [previous] = store.previous_sessions()
    assert previous.session_id == SID
    record = await store.previous_session(SID)
    assert record is not None and "$ exit" in str(record.screen)


async def test_a_deleted_session_leaves_nothing_when_previous_sessions_are_off(tmp_path: Path) -> None:
    store = _store(tmp_path, keep_days=0)
    registry = _registry(tmp_path, store)
    await registry.create_session(_payload())
    await store.save_screen(SID, await _painted(b"$ exit"), [])
    await registry.delete_session(SID)
    assert list((tmp_path / "state" / "live").iterdir()) == []
    assert not (tmp_path / "state" / "previous").exists()


async def test_a_stopping_runtime_saves_its_last_screen_before_its_connector_goes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = _registry(tmp_path, store)
    await registry.create_session(_payload())
    runtime = _runtime(registry)
    connector = MagicMock()
    connector.get_snapshot = AsyncMock(return_value={"screen": "last", "cols": 80, "rows": 24})
    connector.scrollback = MagicMock(return_value=["above"])
    connector.stop = AsyncMock()
    runtime._connector = connector
    await runtime.stop()
    assert _live(tmp_path)["screen"]["ansi"] == "last"
    assert _live(tmp_path)["scrollback"] == ["above"]
    connector.stop.assert_awaited_once()


async def test_a_stopped_runtime_without_a_connector_saves_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = _registry(tmp_path, store)
    await registry.create_session(_payload())
    await _runtime(registry).stop()
    assert _live(tmp_path)["screen"] is None


async def test_an_unread_unrecorded_capture_session_offers_its_keeper_every_pass(tmp_path: Path) -> None:
    # No recording at all: the keeper still hears of the output and is offered
    # the screen on every pass of the loop. When it saves is its own clock's
    # business (test_capture_state.py drives it with explicit times).
    store = _store(tmp_path)
    registry = _registry(tmp_path, store)
    await registry.create_session(_payload())
    rt = _runtime(registry)
    assert rt._logger is None
    keeper = MagicMock()
    keeper.save_if_due = AsyncMock()
    rt._screen_keeper = keeper
    rt._queue = asyncio.Queue()
    rt._stop = asyncio.Event()
    output = [[{"type": "term", "data": "hello"}]]

    async def poll() -> list[dict[str, Any]]:
        await asyncio.sleep(0.01)
        return output.pop(0) if output else []

    connector = MagicMock()
    connector.set_mode = AsyncMock(return_value=[])
    connector.get_snapshot = AsyncMock(return_value={"type": "snapshot", "screen": "hello", "cols": 80, "rows": 24})
    connector.poll_messages = poll
    rt._connector = connector

    class _Ws:
        async def send(self, data: str) -> None:
            return None

        async def recv(self) -> str:
            while output:
                await asyncio.sleep(0.01)
            rt._stop.set()
            await asyncio.sleep(100)
            return ""

    await rt._bridge_session(_Ws())
    keeper.note_output.assert_called_once()
    assert keeper.save_if_due.await_count >= 2
    assert keeper.save_if_due.await_args is not None
    assert keeper.save_if_due.await_args.args[0] is connector


async def test_a_runtime_without_a_keeper_records_under_a_fresh_instance(tmp_path: Path) -> None:
    from provide.uterm.server.models import SessionDefinition

    first = HostedSessionRuntime(
        SessionDefinition(session_id="s"), public_base_url="http://x", recording=RecordingConfig()
    )
    second = HostedSessionRuntime(
        SessionDefinition(session_id="s"), public_base_url="http://x", recording=RecordingConfig()
    )
    assert first._recording_instance != second._recording_instance


# ── the restart ──────────────────────────────────────────────────────────────


async def _before_restart(tmp_path: Path, *, keep_days: int = 7) -> tuple[SessionRegistry, dict[str, Any]]:
    store = _store(tmp_path, keep_days=keep_days)
    registry = _registry(tmp_path, store)
    await registry.create_session(_payload())
    await registry.update_session(SID, {"owner": "blackbetty:7", "visibility": "private"}, allow_owner_change=True)
    snapshot = await _painted(b"\x1b[1mdeck\x1b[0m\nline two\nline three")
    await store.save_screen(SID, snapshot, ["scrolled"])
    # The server stops: its runtimes stop, and nothing is deleted.
    await registry.shutdown()
    return registry, snapshot


async def test_a_live_login_comes_back_under_its_id_with_its_screen_and_owner(tmp_path: Path) -> None:
    before, snapshot = await _before_restart(tmp_path)
    instance = _runtime(before)._recording_instance

    store = _store(tmp_path)
    after = _registry(tmp_path, store)
    with _alive(4242):
        await restore_capture_sessions(after, store, _confined)
    definition = await after.get_definition(SID)
    assert definition is not None
    assert (definition.owner, definition.visibility, definition.connector_config) == (
        "blackbetty:7",
        "private",
        {"socket_path": SOCKET},
    )
    runtime = _runtime(after)
    assert runtime._recording_instance == instance

    # Its first connector is painted with the screen the session showed.
    connector = CaptureConnector(SID, "d", {"socket_path": SOCKET})
    assert runtime._screen_keeper is not None
    runtime._screen_keeper.attach(connector)
    restored = connector._snapshot()
    assert restored["screen"] == snapshot["screen"]
    assert (restored["cols"], restored["rows"]) == (30, 5)
    assert connector.scrollback() == ["scrolled"]


async def test_a_restored_session_keeps_its_recording(tmp_path: Path) -> None:
    await _before_restart(tmp_path)
    recordings = LocalFileRecordingStore(tmp_path / "recordings")
    await recordings.start_session(SID, {"instance": "x"})
    await recordings.end_session(SID)
    store = _store(tmp_path)
    after = _registry(tmp_path, store)
    with _alive(4242):
        await restore_capture_sessions(after, store, _confined)
    assert await recordings.get_path(SID) is not None


async def test_a_restored_session_starts_with_the_others(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = _registry(tmp_path, store)
    await registry.create_session(_payload(auto_start=True))
    await registry.shutdown()
    store = _store(tmp_path)
    after = _registry(tmp_path, store)
    with _alive(4242):
        await restore_capture_sessions(after, store, _confined)
    with patch.object(HostedSessionRuntime, "start", AsyncMock()) as start:
        await after.start_auto_start_sessions()
    start.assert_awaited_once()


async def test_a_login_that_ended_while_the_server_was_down_becomes_a_previous_session(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _before, snapshot = await _before_restart(tmp_path)
    saved_at = _live(tmp_path)["saved_at"]
    store = _store(tmp_path)
    after = _registry(tmp_path, store)
    # The pid now belongs to another process, started at another time.
    with (
        patch("provide.uterm.server.pam_integration._process_start_ticks", return_value=778),
        caplog.at_level(logging.INFO),
    ):
        await restore_capture_sessions(after, store, _confined)
    assert await after.get_definition(SID) is None
    assert "capture_state_login_gone" in caplog.text
    [previous] = store.previous_sessions()
    assert (previous.session_id, previous.ended_at, previous.definition.owner) == (SID, saved_at, "blackbetty:7")
    record = await store.previous_session(SID)
    assert record is not None and record.screen == snapshot["screen"] and record.scrollback == ["scrolled"]
    assert not (tmp_path / "state" / "live" / f"{SID}.json").exists()


async def test_a_dead_login_is_deleted_when_previous_sessions_are_off(tmp_path: Path) -> None:
    await _before_restart(tmp_path, keep_days=0)
    store = _store(tmp_path, keep_days=0)
    after = _registry(tmp_path, store)
    with _alive():
        await restore_capture_sessions(after, store, _confined)
    assert await after.get_definition(SID) is None
    assert list((tmp_path / "state" / "live").iterdir()) == []
    assert not (tmp_path / "state" / "previous").exists()


async def test_a_socket_outside_the_capture_directory_is_not_bound(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await _before_restart(tmp_path)
    store = _store(tmp_path)
    after = _registry(tmp_path, store)

    def elsewhere(socket_path: str) -> bool:
        return capture_socket_allowed(socket_path, PamConfig(capture_socket_dir="/run/other"))

    with _alive(4242), caplog.at_level(logging.WARNING):
        await restore_capture_sessions(after, store, elsewhere)
    assert await after.get_definition(SID) is None
    assert "capture_state_restore_refused" in caplog.text
    assert "reason=socket_not_confined" in caplog.text
    assert [r.session_id for r in store.previous_sessions()] == [SID]


async def test_a_session_that_exists_already_is_left_as_it_is(tmp_path: Path) -> None:
    await _before_restart(tmp_path)
    store = _store(tmp_path)
    after = _registry(tmp_path, store)
    await after.create_session(_payload(display_name="made first"))
    with _alive(4242):
        await restore_capture_sessions(after, store, _confined)
    definition = await after.get_definition(SID)
    assert definition is not None and definition.display_name == "made first"


async def test_a_full_registry_restores_nothing_and_keeps_the_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await _before_restart(tmp_path)
    store = _store(tmp_path)
    after = _registry(tmp_path, store, max_sessions=1)
    await after.create_session(_payload("other"))
    with _alive(4242), caplog.at_level(logging.WARNING):
        await restore_capture_sessions(after, store, _confined)
    assert await after.get_definition(SID) is None
    assert "capture_state_restore_skipped" in caplog.text
    assert "reason=session_limit" in caplog.text
    assert (tmp_path / "state" / "live" / f"{SID}.json").exists()


def _kept_record() -> Any:
    from provide.uterm.server.capture_state import CaptureRecord
    from provide.uterm.server.models import SessionDefinition

    return CaptureRecord(
        definition=SessionDefinition(
            session_id=SID, connector_type="pty_capture", connector_config={"socket_path": SOCKET}
        ),
        recording_instance="i",
    )


async def test_a_full_registry_answers_that_it_adopted_nothing_and_says_why(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = _registry(tmp_path, store, max_sessions=1)
    await registry.create_session(_payload("other"))
    with patch("provide.uterm.server.registry.logger") as logger:
        assert await registry.adopt_restored(_kept_record()) is False
    logger.warning.assert_called_once_with("capture_state_restore_skipped session_id=%s reason=session_limit", SID)


async def test_an_adopted_session_is_answered_as_adopted(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _store(tmp_path))
    assert await registry.adopt_restored(_kept_record()) is True
    assert await registry.get_definition(SID) is not None


async def test_an_id_still_being_torn_down_is_not_restored(tmp_path: Path) -> None:
    await _before_restart(tmp_path)
    store = _store(tmp_path)
    after = _registry(tmp_path, store)
    after._retiring[SID] = asyncio.Event()
    with _alive(4242):
        await restore_capture_sessions(after, store, _confined)
    assert await after.get_definition(SID) is None


async def test_without_persist_capture_only_previous_sessions_are_read(tmp_path: Path) -> None:
    await _before_restart(tmp_path)
    store = _store(tmp_path, persist=False)
    after = _registry(tmp_path, store)
    with _alive(4242):
        await restore_capture_sessions(after, store, _confined)
    assert await after.get_definition(SID) is None
    assert (tmp_path / "state" / "live" / f"{SID}.json").exists()


async def test_no_store_restores_nothing(tmp_path: Path) -> None:
    registry = _registry(tmp_path, None)
    await restore_capture_sessions(registry, None, _confined)
    assert await registry.list_sessions() == []


@pytest.mark.parametrize(
    ("session_id", "ticks", "alive"),
    [(SID, 777, True), (SID, 778, False), (SID, None, False), ("provide-shell", 777, False)],
)
def test_a_login_is_alive_only_while_its_pid_reports_the_start_its_id_names(
    session_id: str, ticks: int | None, alive: bool
) -> None:
    with patch("provide.uterm.server.pam_integration._process_start_ticks", return_value=ticks):
        assert cs.login_is_alive(session_id) is alive


async def test_previous_sessions_are_pruned_hourly(tmp_path: Path) -> None:
    from provide.uterm.server.app import factory_sweeps

    store = MagicMock()
    store.keeps_previous = True
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    with patch.object(factory_sweeps.asyncio, "sleep", sleep), pytest.raises(asyncio.CancelledError):
        await factory_sweeps.sweep_previous_sessions(store=store)
    assert sleeps == [3600.0, 3600.0, 3600.0]
    assert store.prune.call_count == 2


@pytest.mark.parametrize("store", [None, MagicMock(keeps_previous=False)])
async def test_nothing_is_pruned_without_previous_sessions(store: Any) -> None:
    from provide.uterm.server.app import factory_sweeps

    calls = 0

    async def sleep(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError

    with patch.object(factory_sweeps.asyncio, "sleep", sleep), pytest.raises(asyncio.CancelledError):
        await factory_sweeps.sweep_previous_sessions(store=store)
    if store is not None:
        store.prune.assert_not_called()


async def test_a_started_connector_is_readied_by_the_keeper(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = _registry(tmp_path, store)
    await registry.create_session(_payload())
    runtime = _runtime(registry)
    connector = MagicMock()
    connector.start = AsyncMock()
    connector.is_connected = MagicMock(return_value=True)
    with patch("provide.uterm.server.runtime.build_connector", return_value=connector):
        assert await runtime._start_connector() is connector
    connector.keep_scrollback.assert_called_once_with(100, [])


async def test_a_registry_that_keeps_no_capture_sessions_adopts_none(tmp_path: Path) -> None:
    from provide.uterm.server.capture_state import CaptureRecord
    from provide.uterm.server.models import SessionDefinition

    record = CaptureRecord(
        definition=SessionDefinition(
            session_id=SID, connector_type="pty_capture", connector_config={"socket_path": SOCKET}
        ),
        recording_instance="i",
    )
    registry = _registry(tmp_path, None)
    assert await registry.adopt_restored(record) is False
    assert await registry.get_definition(SID) is None
