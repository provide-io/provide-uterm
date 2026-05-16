#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for server/runtime.py."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime


def _make_session(
    session_id: str = "s1",
    display_name: str = "Test",
    connector_type: str = "shell",
    input_mode: str = "hijack",
    auto_start: bool = False,
    tags: list[str] | None = None,
    owner: str | None = None,
    visibility: str = "private",
) -> SessionDefinition:
    kwargs: dict[str, Any] = {
        "session_id": session_id,
        "display_name": display_name,
        "connector_type": connector_type,
        "input_mode": input_mode,  # type: ignore[arg-type]
        "auto_start": auto_start,
        "tags": tags or [],
        "visibility": visibility,  # type: ignore[arg-type]
    }
    if owner is not None:
        kwargs["owner"] = owner
    return SessionDefinition(**kwargs)


def _make_runtime(
    session_id: str = "s1",
    base_url: str = "http://localhost:9999",
    bearer_token: str | None = None,
    recording: RecordingConfig | None = None,
) -> HostedSessionRuntime:
    return HostedSessionRuntime(
        _make_session(session_id=session_id),
        public_base_url=base_url,
        recording=recording or RecordingConfig(),
        worker_bearer_token=bearer_token,
    )


# ---------------------------------------------------------------------------
# status() — field correctness
# ---------------------------------------------------------------------------


class TestStatusFields:
    def test_session_id_from_definition(self) -> None:
        """mut_1: session_id=None."""
        rt = _make_runtime(session_id="my-id")
        assert rt.status().session_id == "my-id"

    def test_display_name_from_definition(self) -> None:
        """mut_2: display_name=None."""
        sess = _make_session(display_name="My Session")
        rt = HostedSessionRuntime(sess, public_base_url="http://x:1", recording=RecordingConfig())
        assert rt.status().display_name == "My Session"

    def test_connector_type_from_definition(self) -> None:
        """mut_4: connector_type=None."""
        sess = _make_session(connector_type="shell")
        rt = HostedSessionRuntime(sess, public_base_url="http://x:1", recording=RecordingConfig())
        assert rt.status().connector_type == "shell"

    def test_lifecycle_state_is_stopped_initially(self) -> None:
        """mut_5: lifecycle_state=None."""
        rt = _make_runtime()
        assert rt.status().lifecycle_state == "stopped"

    def test_input_mode_from_definition(self) -> None:
        """mut_20: input_mode key dropped."""
        sess = _make_session(input_mode="open")
        rt = HostedSessionRuntime(sess, public_base_url="http://x:1", recording=RecordingConfig())
        assert rt.status().input_mode == "open"

    def test_connected_false_initially(self) -> None:
        """mut_8: connected=None."""
        rt = _make_runtime()
        assert rt.status().connected is False

    def test_auto_start_from_definition(self) -> None:
        """mut_10: auto_start=None."""
        sess = _make_session(auto_start=True)
        rt = HostedSessionRuntime(sess, public_base_url="http://x:1", recording=RecordingConfig())
        assert rt.status().auto_start is True

    def test_tags_is_list(self) -> None:
        """mut_11/12: tags=None or not list()."""
        sess = _make_session(tags=["a", "b"])
        rt = HostedSessionRuntime(sess, public_base_url="http://x:1", recording=RecordingConfig())
        s = rt.status()
        assert isinstance(s.tags, list)
        assert "a" in s.tags

    def test_recording_enabled_in_status(self) -> None:
        """mut_13: recording_enabled omitted."""
        rt = _make_runtime()
        s = rt.status()
        assert s.recording_enabled is not None
        assert isinstance(s.recording_enabled, bool)

    def test_recording_available_false_when_no_path(self) -> None:
        """mut_25: recording_available omitted."""
        rt = _make_runtime()
        s = rt.status()
        # No recording path set → False
        assert s.recording_available is False

    def test_last_error_none_initially(self) -> None:
        """mut_14/28: last_error=None or omitted."""
        rt = _make_runtime()
        s = rt.status()
        assert s.last_error is None

    def test_last_error_reflects_runtime_state(self) -> None:
        """Verify last_error is actually taken from _last_error."""
        rt = _make_runtime()
        rt._last_error = "connection refused"
        assert rt.status().last_error == "connection refused"

    def test_owner_from_definition(self) -> None:
        """mut: owner field preserved."""
        sess = _make_session(owner="alice")
        rt = HostedSessionRuntime(sess, public_base_url="http://x:1", recording=RecordingConfig())
        assert rt.status().owner == "alice"

    def test_visibility_from_definition(self) -> None:
        """mut: visibility field preserved."""
        sess = _make_session(visibility="public")
        rt = HostedSessionRuntime(sess, public_base_url="http://x:1", recording=RecordingConfig())
        assert rt.status().visibility == "public"


# ---------------------------------------------------------------------------
# start() — guard and state transitions
# ---------------------------------------------------------------------------


class TestStart:
    async def test_start_sets_state_to_starting(self) -> None:
        """mut_8/9/10: _state=None/'XXstartingXX'/'STARTING'."""
        rt = _make_runtime()
        # Prevent the task from actually running
        with patch.object(type(rt), "_run", _make_noop_run()):
            await rt.start()
        assert rt._state == "starting"
        if rt._task:
            rt._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await rt._task

    async def test_start_sets_last_error_to_none(self) -> None:
        """mut_11: _last_error=''."""
        rt = _make_runtime()
        rt._last_error = "previous error"
        with patch.object(type(rt), "_run", _make_noop_run()):
            await rt.start()
        assert rt._last_error is None
        if rt._task:
            rt._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await rt._task

    async def test_start_creates_task(self) -> None:
        """mut_12/13: _task=None or create_task(None)."""
        rt = _make_runtime()
        with patch.object(type(rt), "_run", _make_noop_run()):
            await rt.start()
        assert rt._task is not None
        # Clean up
        rt._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await rt._task

    async def test_start_sets_queue(self) -> None:
        """mut_5/6: _queue=None or Queue(maxsize=None)."""
        rt = _make_runtime()
        with patch.object(type(rt), "_run", _make_noop_run()):
            await rt.start()
        assert rt._queue is not None
        assert isinstance(rt._queue, asyncio.Queue)
        if rt._task:
            rt._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await rt._task

    async def test_start_not_reentrant_when_task_running(self) -> None:
        """mut_1/2/3: 'and' → 'or', 'is not None' → 'is None', 'not done' → 'done'."""
        rt = _make_runtime()
        # Start once
        with patch.object(type(rt), "_run", _make_noop_run()):
            await rt.start()
        first_task = rt._task
        # Start again — should NOT create a new task if first is still running
        await rt.start()
        assert rt._task is first_task
        if first_task:
            first_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await first_task


async def _noop_coro() -> None:
    """A coroutine that never completes (used to mock _run)."""
    await asyncio.Event().wait()


def _make_noop_run():
    """Return an async method that parks until cancelled."""

    async def _run(self):
        await asyncio.Event().wait()

    return _run


# ---------------------------------------------------------------------------
# stop() — state transitions
# ---------------------------------------------------------------------------


class TestStop:
    async def test_stop_sets_state_to_stopped(self) -> None:
        """mut_5/6/7: _state=None/'XXstoppedXX'/'STOPPED'."""
        rt = _make_runtime()
        await rt.stop()
        assert rt._state == "stopped"

    async def test_stop_sets_connected_to_false(self) -> None:
        """mut_8/9: _connected=None/True."""
        rt = _make_runtime()
        rt._connected = True
        await rt.stop()
        assert rt._connected is False

    async def test_stop_clears_task(self) -> None:
        """mut_4: _task='' instead of None."""
        rt = _make_runtime()
        with patch.object(type(rt), "_run", _make_noop_run()):
            await rt.start()
        await rt.stop()
        assert rt._task is None


# ---------------------------------------------------------------------------
# set_mode() — validation and stored mode
# ---------------------------------------------------------------------------


class TestSetMode:
    async def test_hijack_mode_accepted(self) -> None:
        """mut_2/3: 'hijack' renamed to 'XXhijackXX'/'HIJACK'."""
        rt = _make_runtime()
        await rt.set_mode("hijack")
        assert rt.definition.input_mode == "hijack"

    async def test_open_mode_accepted(self) -> None:
        """mut_4/5: 'open' renamed to 'XXopenXX'/'OPEN'."""
        rt = _make_runtime()
        await rt.set_mode("open")
        assert rt.definition.input_mode == "open"

    async def test_invalid_mode_raises_value_error(self) -> None:
        """mut_1: 'not in' → 'in' flips guard logic."""
        rt = _make_runtime()
        with pytest.raises(ValueError, match="invalid mode"):
            await rt.set_mode("superuser")

    async def test_set_mode_calls_connector_when_connected(self) -> None:
        """mut_7/8/9/10/11: typed_mode=None or cast args mutated."""
        rt = _make_runtime()
        connector = MagicMock()
        connector.set_mode = AsyncMock(return_value=[])
        rt._connector = connector
        rt._queue = asyncio.Queue()
        await rt.set_mode("open")
        connector.set_mode.assert_awaited_once_with("open")

    async def test_set_mode_no_connector_no_error(self) -> None:
        """Verify early return when _connector is None."""
        rt = _make_runtime()
        rt._connector = None
        # Should not raise
        await rt.set_mode("hijack")


# ---------------------------------------------------------------------------
# _run() — state changes on ValueError (permanent failure)
# ---------------------------------------------------------------------------


class TestRunPermanentFailure:
    async def test_value_error_sets_state_error(self) -> None:
        """mut_28/29/30: _state=None/'XXerrorXX'/'ERROR' on ValueError."""
        rt = _make_runtime()
        state_on_error: list[str] = []

        original_log_event = rt._log_event

        async def _capture_state(event, payload):
            # Called right after _state = "error"
            state_on_error.append(rt._state)
            await original_log_event(event, payload)

        async def _fail_connector():
            raise ValueError("bad connector type")

        with (
            patch.object(rt, "_start_connector", _fail_connector),
            patch.object(rt, "_log_event", _capture_state),
        ):
            rt._stop = asyncio.Event()
            rt._queue = asyncio.Queue()
            await rt._run()

        # During ValueError handling, _state must have been "error"
        assert "error" in state_on_error
        assert rt._last_error == "bad connector type"

    async def test_value_error_sets_last_error(self) -> None:
        """mut_33/34: _last_error=None/str(None)."""
        rt = _make_runtime()

        exc_msg = "missing known_hosts"

        async def _fail_connector():
            raise ValueError(exc_msg)

        with patch.object(rt, "_start_connector", _fail_connector):
            rt._stop = asyncio.Event()
            rt._queue = asyncio.Queue()
            await rt._run()

        assert rt._last_error == exc_msg

    async def test_value_error_breaks_loop(self) -> None:
        """mut_27: return instead of break — _run must return, not loop."""
        call_count = 0
        rt = _make_runtime()

        async def _fail_connector():
            nonlocal call_count
            call_count += 1
            raise ValueError("permanent")

        with patch.object(rt, "_start_connector", _fail_connector):
            rt._stop = asyncio.Event()
            rt._queue = asyncio.Queue()
            await rt._run()

        # Must only be called once — break exits the loop
        assert call_count == 1

    async def test_run_sets_final_state_stopped(self) -> None:
        """mut_129: _state=None at end of _run."""
        rt = _make_runtime()

        async def _fail_connector():
            raise ValueError("bad")

        with patch.object(rt, "_start_connector", _fail_connector):
            rt._stop = asyncio.Event()
            rt._queue = asyncio.Queue()
            await rt._run()

        assert rt._state == "stopped"


# (TestRunTransientFailure, TestRunAuthHeader, TestRunFinally moved to test_runtime_mutant_kills_2.py)
