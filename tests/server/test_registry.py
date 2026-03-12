#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for server registry.py — SessionRegistry methods not covered by API route tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.server.models import RecordingConfig, SessionDefinition
from provide.terminal.server.registry import SessionRegistry, SessionValidationError


def _make_hub() -> MagicMock:
    hub = MagicMock()
    hub.force_release_hijack = AsyncMock(return_value=True)
    hub.get_last_snapshot = AsyncMock(return_value=None)
    hub.get_recent_events = AsyncMock(return_value=[])
    hub.browser_count = AsyncMock(return_value=0)
    hub.on_worker_empty = None
    return hub


def _make_registry(
    sessions: list[SessionDefinition] | None = None,
    *,
    recording: RecordingConfig | None = None,
) -> SessionRegistry:
    hub = _make_hub()
    return SessionRegistry(
        sessions or [],
        hub=hub,
        public_base_url="http://localhost:9999",
        recording=recording or RecordingConfig(),
    )


def _session(
    session_id: str = "sess1",
    auto_start: bool = False,
    ephemeral: bool = False,
    owner: str | None = None,
) -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        display_name=f"Session {session_id}",
        connector_type="shell",
        auto_start=auto_start,
        ephemeral=ephemeral,
        owner=owner,
    )


# ---------------------------------------------------------------------------
# _require_session
# ---------------------------------------------------------------------------


class TestRequireSession:
    async def test_unknown_session_raises_key_error(self) -> None:
        reg = _make_registry()
        async with reg._lock:
            with pytest.raises(KeyError, match="unknown session"):
                reg._require_session("does-not-exist")

    async def test_known_session_returned(self) -> None:
        reg = _make_registry([_session("s1")])
        async with reg._lock:
            defn = reg._require_session("s1")
        assert defn.session_id == "s1"


# ---------------------------------------------------------------------------
# list_sessions (not wired to API — tests the method directly)
# ---------------------------------------------------------------------------


class TestListSessions:
    async def test_empty_registry(self) -> None:
        reg = _make_registry()
        result = await reg.list_sessions()
        assert result == []

    async def test_returns_status_for_each_session(self) -> None:
        reg = _make_registry([_session("a"), _session("b")])
        result = await reg.list_sessions()
        ids = {r.session_id for r in result}
        assert ids == {"a", "b"}


# ---------------------------------------------------------------------------
# start_auto_start_sessions
# ---------------------------------------------------------------------------


class TestStartAutoStartSessions:
    async def test_auto_start_sessions_are_started(self) -> None:
        sessions = [
            _session("s1", auto_start=True),
            _session("s2", auto_start=False),
        ]
        reg = _make_registry(sessions)
        started: list[str] = []

        async def _mock_start(session_id: str):  # type: ignore[return]
            started.append(session_id)
            return MagicMock()

        with patch.object(reg, "start_session", side_effect=_mock_start):
            await reg.start_auto_start_sessions()

        assert started == ["s1"]

    async def test_no_auto_start_sessions(self) -> None:
        reg = _make_registry([_session("s1", auto_start=False)])
        with patch.object(reg, "start_session") as mock_start:
            await reg.start_auto_start_sessions()
            mock_start.assert_not_called()


# ---------------------------------------------------------------------------
# _on_worker_empty — ephemeral session cleanup
# ---------------------------------------------------------------------------


class TestOnWorkerEmpty:
    async def test_ephemeral_session_deleted(self) -> None:
        reg = _make_registry([_session("ephem", ephemeral=True)])
        assert await reg.get_definition("ephem") is not None
        await reg._on_worker_empty("ephem")
        # Session should be gone — _on_worker_empty inlines the delete
        assert await reg.get_definition("ephem") is None

    async def test_non_ephemeral_session_not_deleted(self) -> None:
        reg = _make_registry([_session("perm", ephemeral=False)])
        with patch.object(reg, "delete_session") as mock_del:
            await reg._on_worker_empty("perm")
            mock_del.assert_not_called()

    async def test_unknown_session_is_noop(self) -> None:
        reg = _make_registry()
        # Should not raise even if session doesn't exist
        await reg._on_worker_empty("no-such-session")


# ---------------------------------------------------------------------------
# create_session — validation errors
# ---------------------------------------------------------------------------


class TestCreateSessionValidation:
    async def test_invalid_session_id_raises(self) -> None:
        reg = _make_registry()
        with pytest.raises(SessionValidationError, match="session_id must match"):
            await reg.create_session({"session_id": "bad id!"})

    async def test_invalid_input_mode_raises(self) -> None:
        reg = _make_registry()
        with pytest.raises(SessionValidationError, match="input_mode must be"):
            await reg.create_session({"session_id": "valid-id", "input_mode": "superuser"})

    async def test_invalid_visibility_raises(self) -> None:
        reg = _make_registry()
        with pytest.raises(SessionValidationError, match="visibility must be"):
            await reg.create_session({"session_id": "valid-id", "visibility": "secret"})

    async def test_duplicate_session_raises_value_error(self) -> None:
        reg = _make_registry([_session("existing")])
        with pytest.raises(ValueError, match="session already exists"):
            await reg.create_session({"session_id": "existing"})

    async def test_auto_start_triggers_runtime_start(self) -> None:
        reg = _make_registry()
        mock_runtime = AsyncMock()
        mock_runtime.status.return_value = MagicMock(session_id="auto-s")

        with patch("provide.terminal.server.registry.HostedSessionRuntime", return_value=mock_runtime):
            await reg.create_session({"session_id": "auto-s", "auto_start": True})

        mock_runtime.start.assert_called_once()


# ---------------------------------------------------------------------------
# update_session — field update coverage
# ---------------------------------------------------------------------------


class TestUpdateSession:
    async def test_update_display_name(self) -> None:
        reg = _make_registry([_session("s1")])
        status = await reg.update_session("s1", {"display_name": "New Name"})
        assert status.display_name == "New Name"

    async def test_update_input_mode(self) -> None:
        reg = _make_registry([_session("s1")])
        status = await reg.update_session("s1", {"input_mode": "open"})
        assert status.session_id == "s1"

    async def test_update_invalid_input_mode_raises(self) -> None:
        reg = _make_registry([_session("s1")])
        with pytest.raises(SessionValidationError, match="input_mode"):
            await reg.update_session("s1", {"input_mode": "invalid"})

    async def test_update_visibility(self) -> None:
        reg = _make_registry([_session("s1")])
        await reg.update_session("s1", {"visibility": "private"})
        async with reg._lock:
            defn = reg._require_session("s1")
        assert defn.visibility == "private"

    async def test_update_invalid_visibility_raises(self) -> None:
        reg = _make_registry([_session("s1")])
        with pytest.raises(SessionValidationError, match="visibility"):
            await reg.update_session("s1", {"visibility": "secret"})

    async def test_update_auto_start(self) -> None:
        reg = _make_registry([_session("s1")])
        await reg.update_session("s1", {"auto_start": True})
        async with reg._lock:
            defn = reg._require_session("s1")
        assert defn.auto_start is True

    async def test_update_tags(self) -> None:
        reg = _make_registry([_session("s1")])
        await reg.update_session("s1", {"tags": ["foo", "bar"]})
        async with reg._lock:
            defn = reg._require_session("s1")
        assert defn.tags == ["foo", "bar"]

    async def test_update_recording_enabled(self) -> None:
        reg = _make_registry([_session("s1")])
        await reg.update_session("s1", {"recording_enabled": True})
        async with reg._lock:
            defn = reg._require_session("s1")
        assert defn.recording_enabled is True

    async def test_update_connector_config(self) -> None:
        reg = _make_registry([_session("s1")])
        await reg.update_session("s1", {"connector_config": {"host": "newhost"}})
        async with reg._lock:
            defn = reg._require_session("s1")
        assert defn.connector_config.get("host") == "newhost"

    async def test_update_unknown_session_raises(self) -> None:
        reg = _make_registry()
        with pytest.raises(KeyError):
            await reg.update_session("no-such", {"display_name": "x"})


# ---------------------------------------------------------------------------
# recording_entries — offset branch and event filter + bad JSON in tail
# ---------------------------------------------------------------------------


class TestRecordingEntries:
    async def test_no_recording_path_returns_empty(self) -> None:
        reg = _make_registry([_session("s1")])
        result = await reg.recording_entries("s1")
        assert result == []

    async def test_tail_mode_returns_last_entries(self, tmp_path: Path) -> None:
        recording = RecordingConfig(directory=tmp_path)
        reg = _make_registry([_session("s1")], recording=recording)
        path = tmp_path / "s1.jsonl"
        entries = [{"event": "screen", "i": i} for i in range(5)]
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        # Patch runtime to expose the path
        runtime = reg._runtimes.setdefault("s1", reg._runtime_for(reg._sessions["s1"]))
        runtime._recording_path = path

        result = await reg.recording_entries("s1", limit=3)
        assert len(result) == 3
        assert result[-1]["i"] == 4  # last 3 entries

    async def test_offset_mode_skips_entries(self, tmp_path: Path) -> None:
        recording = RecordingConfig(directory=tmp_path)
        reg = _make_registry([_session("s1")], recording=recording)
        path = tmp_path / "s1.jsonl"
        entries = [{"event": "screen", "i": i} for i in range(5)]
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        runtime = reg._runtime_for(reg._sessions["s1"])
        runtime._recording_path = path

        result = await reg.recording_entries("s1", offset=2, limit=10)
        assert len(result) == 3
        assert result[0]["i"] == 2

    async def test_event_filter_in_tail_mode(self, tmp_path: Path) -> None:
        recording = RecordingConfig(directory=tmp_path)
        reg = _make_registry([_session("s1")], recording=recording)
        path = tmp_path / "s1.jsonl"
        lines = [
            json.dumps({"event": "screen", "i": 0}),
            json.dumps({"event": "input", "i": 1}),
            json.dumps({"event": "screen", "i": 2}),
        ]
        path.write_text("\n".join(lines) + "\n")

        runtime = reg._runtime_for(reg._sessions["s1"])
        runtime._recording_path = path

        result = await reg.recording_entries("s1", event="screen")
        assert len(result) == 2
        assert all(e["event"] == "screen" for e in result)

    async def test_bad_json_lines_skipped_in_tail_mode(self, tmp_path: Path) -> None:
        recording = RecordingConfig(directory=tmp_path)
        reg = _make_registry([_session("s1")], recording=recording)
        path = tmp_path / "s1.jsonl"
        path.write_text('{"event": "ok"}\nnot-json\n{"event": "also-ok"}\n')

        runtime = reg._runtime_for(reg._sessions["s1"])
        runtime._recording_path = path

        result = await reg.recording_entries("s1")
        assert len(result) == 2
        assert all(e["event"] in {"ok", "also-ok"} for e in result)

    async def test_bad_json_lines_skipped_in_offset_mode(self, tmp_path: Path) -> None:
        recording = RecordingConfig(directory=tmp_path)
        reg = _make_registry([_session("s1")], recording=recording)
        path = tmp_path / "s1.jsonl"
        path.write_text('{"event": "a"}\nnot-json\n{"event": "b"}\n')

        runtime = reg._runtime_for(reg._sessions["s1"])
        runtime._recording_path = path

        result = await reg.recording_entries("s1", offset=0)
        assert len(result) == 2

    async def test_event_filter_in_offset_mode(self, tmp_path: Path) -> None:
        recording = RecordingConfig(directory=tmp_path)
        reg = _make_registry([_session("s1")], recording=recording)
        path = tmp_path / "s1.jsonl"
        lines = [
            json.dumps({"event": "screen", "i": 0}),
            json.dumps({"event": "input", "i": 1}),
            json.dumps({"event": "screen", "i": 2}),
        ]
        path.write_text("\n".join(lines) + "\n")

        runtime = reg._runtime_for(reg._sessions["s1"])
        runtime._recording_path = path

        result = await reg.recording_entries("s1", offset=0, event="screen")
        assert len(result) == 2
        assert all(e["event"] == "screen" for e in result)

    async def test_blank_lines_skipped_in_offset_mode(self, tmp_path: Path) -> None:
        recording = RecordingConfig(directory=tmp_path)
        reg = _make_registry([_session("s1")], recording=recording)
        path = tmp_path / "s1.jsonl"
        path.write_text('{"event": "a"}\n\n{"event": "b"}\n')

        runtime = reg._runtime_for(reg._sessions["s1"])
        runtime._recording_path = path

        result = await reg.recording_entries("s1", offset=0)
        assert len(result) == 2

    async def test_blank_lines_skipped_in_tail_mode(self, tmp_path: Path) -> None:
        recording = RecordingConfig(directory=tmp_path)
        reg = _make_registry([_session("s1")], recording=recording)
        path = tmp_path / "s1.jsonl"
        path.write_text('{"event": "a"}\n\n{"event": "b"}\n')

        runtime = reg._runtime_for(reg._sessions["s1"])
        runtime._recording_path = path

        result = await reg.recording_entries("s1")
        assert len(result) == 2
