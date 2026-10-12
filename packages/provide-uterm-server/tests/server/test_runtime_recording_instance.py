#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A session reusing an id is never handed the earlier session's recording.

Recording reads are authorized against the session that holds the id *now*, so
a new session under a reused id (a PAM capture session named after a recycled
pid) must start its own recording. A reconnect of the same session -- one
runtime opening a recording per worker connection -- still continues it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from provide.uterm.recording import InMemoryRecordingStore, LocalFileRecordingStore, RecordingStore
from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime

SESSION_ID = "pam-suokki-capture-4242"


def _connector() -> MagicMock:
    connector = MagicMock()
    connector.is_connected = MagicMock(return_value=False)
    connector.start = AsyncMock()
    connector.stop = AsyncMock()
    return connector


def _runtime(store: RecordingStore) -> HostedSessionRuntime:
    definition = SessionDefinition(
        session_id=SESSION_ID, display_name="x", connector_type="shell", auto_start=False, recording_enabled=True
    )
    return HostedSessionRuntime(
        definition, public_base_url="http://localhost:9999", recording=RecordingConfig(), recording_store=store
    )


async def _one_connection(runtime: HostedSessionRuntime, text: str) -> None:
    await runtime._start_recording()
    await runtime._log_event("read", {"text": text})
    await runtime._stop_recording()


def _texts(entries: list[dict[str, Any]]) -> list[str]:
    return [e["data"]["text"] for e in entries if e["event"] == "read"]


async def _check(store: RecordingStore) -> None:
    with patch("provide.uterm.server.runtime.build_connector", return_value=_connector()):
        first = _runtime(store)
        await _one_connection(first, "alice-1")
        await _one_connection(first, "alice-2")
        assert _texts(await store.get_entries(SESSION_ID, limit=500)) == ["alice-1", "alice-2"]

        # The first session is gone; a different caller's session takes the id.
        second = _runtime(store)
        await _one_connection(second, "bob")
    assert _texts(await store.get_entries(SESSION_ID, limit=500)) == ["bob"]


async def test_a_reused_id_does_not_inherit_the_file_recording(tmp_path: Path) -> None:
    await _check(LocalFileRecordingStore(tmp_path))


async def test_a_reused_id_does_not_inherit_the_memory_recording() -> None:
    await _check(InMemoryRecordingStore())


def test_each_runtime_is_its_own_recording_instance() -> None:
    store = InMemoryRecordingStore()
    assert _runtime(store)._recording_instance != _runtime(store)._recording_instance
