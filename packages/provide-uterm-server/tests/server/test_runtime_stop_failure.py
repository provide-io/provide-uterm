#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""stop() releases the connector and settles the state even when closing the recording fails.

A final recording flush that raised (disk full, EACCES) used to escape stop()
before the connector was discarded or the state updated: the connector's
process leaked and the status kept reading running/connected.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server import runtime as runtime_module
from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime


def _runtime() -> tuple[HostedSessionRuntime, AsyncMock, AsyncMock]:
    runtime = HostedSessionRuntime(
        SessionDefinition(session_id="cap-1", display_name="Capture", connector_type="shell", auto_start=False),
        public_base_url="http://localhost:9999",
        recording=RecordingConfig(),
    )
    logger = AsyncMock()
    connector = AsyncMock()
    runtime._logger = logger
    runtime._connector = connector
    runtime._state = "running"
    runtime._connected = True
    runtime._escape_carry = "\x1b["
    runtime._read_annotation_keys.add("k")
    return runtime, logger, connector


def _assert_down(runtime: HostedSessionRuntime, connector: AsyncMock) -> None:
    connector.stop.assert_awaited_once()
    assert runtime._connector is None
    assert runtime._logger is None
    assert runtime._recording_path is None
    assert runtime._escape_carry == ""
    assert runtime._read_annotation_keys == set()
    assert runtime._task is None
    status = runtime.status()
    assert status.lifecycle_state == "stopped"
    assert status.connected is False
    assert status.stopped_at is not None


async def test_a_failed_final_flush_still_stops_the_connector() -> None:
    runtime, logger, connector = _runtime()
    logger.stop.side_effect = OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        await runtime.stop()

    logger.stop.assert_awaited_once()
    _assert_down(runtime, connector)
    assert runtime.status().last_error == "disk full"


async def test_a_failed_run_task_is_surfaced_after_everything_is_down() -> None:
    runtime, logger, connector = _runtime()

    async def _boom() -> None:
        raise RuntimeError("run loop died")

    runtime._task = asyncio.create_task(_boom())
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="run loop died"):
        await runtime.stop()

    logger.stop.assert_awaited_once()
    _assert_down(runtime, connector)
    assert runtime.status().last_error == "run loop died"


async def test_the_first_of_two_failures_is_the_one_raised() -> None:
    runtime, logger, connector = _runtime()
    logger.stop.side_effect = OSError("disk full")

    async def _boom() -> None:
        raise RuntimeError("run loop died")

    runtime._task = asyncio.create_task(_boom())
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="run loop died"):
        await runtime.stop()

    _assert_down(runtime, connector)
    assert runtime.status().last_error == "run loop died"


async def test_a_clean_stop_raises_nothing_and_keeps_no_error() -> None:
    runtime, logger, connector = _runtime()

    await runtime.stop()

    logger.stop.assert_awaited_once()
    _assert_down(runtime, connector)
    assert runtime.status().last_error is None


async def test_the_run_loop_end_releases_the_connector_when_the_recording_fails() -> None:
    runtime, logger, connector = _runtime()
    logger.stop.side_effect = OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        await runtime._stop_connector()

    connector.stop.assert_awaited_once()
    assert runtime._connector is None
    assert runtime._logger is None
    assert runtime._escape_carry == ""
    assert runtime._read_annotation_keys == set()


# ---------------------------------------------------------------------------
# The run loop survives a failed per-connection recording close
# ---------------------------------------------------------------------------


async def test_a_failed_close_after_a_clean_connection_end_is_recorded_not_raised() -> None:
    runtime, logger, connector = _runtime()
    logger.stop.side_effect = OSError("disk full")

    await runtime._end_connection_recording(None)

    assert runtime._logger is None
    assert runtime._escape_carry == ""
    assert runtime._read_annotation_keys == set()
    assert runtime._state == "running"  # untouched: the loop sets its own state
    assert runtime.status().last_error == "recording close failed: disk full"
    assert runtime._pending_close_failure is None  # not stopping: nothing to raise
    connector.stop.assert_not_awaited()  # the connector outlives the connection


async def test_a_failed_close_keeps_the_error_that_ended_the_connection() -> None:
    runtime, logger, _ = _runtime()
    logger.stop.side_effect = OSError("disk full")
    runtime._last_error = "worker gone"

    await runtime._end_connection_recording("worker gone")

    assert runtime.status().last_error == "worker gone; recording close failed: disk full"


async def test_a_failed_close_while_stopping_is_kept_for_stop() -> None:
    runtime, logger, _ = _runtime()
    failure = OSError("disk full")
    logger.stop.side_effect = failure
    runtime._stop.set()

    await runtime._end_connection_recording(None)

    assert runtime._pending_close_failure is failure


async def test_a_clean_connection_recording_close_leaves_state_alone() -> None:
    runtime, logger, _ = _runtime()

    await runtime._end_connection_recording(None)

    logger.stop.assert_awaited_once()
    assert runtime._state == "running"
    assert runtime.status().last_error is None
    assert runtime._pending_close_failure is None


async def test_a_failed_runtime_error_write_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, logger, _ = _runtime()
    failure = OSError("disk full")
    logger.log_event.side_effect = failure
    recorder = MagicMock()
    monkeypatch.setattr(runtime_module, "logger", recorder)

    await runtime._log_runtime_error({"error": "worker gone"})

    logger.log_event.assert_awaited_once_with("runtime_error", {"error": "worker gone"})
    recorder.warning.assert_called_once_with(
        "hosted_session_runtime_error_log_failed session_id=%s error=%s", "cap-1", failure
    )


class _FakeConnection:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    runtime: HostedSessionRuntime,
    loggers: list[AsyncMock],
    make_logger: Any,
    bridge: Any,
) -> None:
    import websockets

    async def _start_recording() -> None:
        logger = make_logger(len(loggers))
        loggers.append(logger)
        runtime._logger = logger

    monkeypatch.setattr(runtime, "_start_recording", _start_recording)
    monkeypatch.setattr(runtime, "_bridge_session", bridge)
    monkeypatch.setattr(websockets, "connect", lambda *_a, **_k: _FakeConnection())


async def test_the_run_loop_survives_a_store_that_keeps_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The connection fails, its runtime_error write fails and its close fails.

    The runtime_error write used to re-raise inside the run loop's ``except``
    and kill ``_run``: no reconnect, connector leaked. Now the loop reconnects,
    keeps the connection's own error, and releases the connector at the end.
    """
    runtime, _, connector = _runtime()
    runtime._logger = None
    runtime._stop = asyncio.Event()
    connector.is_connected = lambda: True
    loggers: list[AsyncMock] = []

    def _make_logger(index: int) -> AsyncMock:
        logger = AsyncMock()
        if index == 0:
            logger.log_event.side_effect = OSError("disk full")
            logger.stop.side_effect = OSError("disk full")
        return logger

    connections = 0

    async def _bridge(_ws: object) -> None:
        nonlocal connections
        connections += 1
        if connections == 1:
            raise RuntimeError("worker gone")
        runtime._stop.set()

    _wire(monkeypatch, runtime, loggers, _make_logger, _bridge)

    await runtime._run()

    assert connections == 2  # it reconnected
    loggers[0].log_event.assert_awaited_once_with("runtime_error", {"error": "worker gone"})
    assert [logger.stop.await_count for logger in loggers] == [1, 1]
    connector.stop.assert_awaited_once()
    assert runtime._connector is None
    assert runtime._state == "stopped"
    assert runtime.status().last_error == "worker gone; recording close failed: disk full"


async def test_a_permanent_failure_with_a_failing_store_still_releases_the_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, connector = _runtime()
    runtime._logger = None
    runtime._stop = asyncio.Event()
    connector.is_connected = lambda: True
    loggers: list[AsyncMock] = []

    def _make_logger(_index: int) -> AsyncMock:
        logger = AsyncMock()
        logger.log_event.side_effect = OSError("disk full")
        return logger

    async def _bridge(_ws: object) -> None:
        raise ValueError("bad config")

    _wire(monkeypatch, runtime, loggers, _make_logger, _bridge)

    await runtime._run()

    loggers[0].log_event.assert_awaited_once_with("runtime_error", {"error": "bad config", "permanent": True})
    connector.stop.assert_awaited_once()
    assert runtime._state == "stopped"
    assert runtime.status().last_error == "bad config"


async def test_stop_raises_a_close_failure_hit_while_stopping_the_run_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run loop swallows a close failure so it can reach cleanup; stop() then raises it."""
    runtime, _, connector = _runtime()
    runtime._logger = None
    connector.is_connected = lambda: True
    loggers: list[AsyncMock] = []
    bridged = asyncio.Event()

    def _make_logger(_index: int) -> AsyncMock:
        logger = AsyncMock()
        logger.stop.side_effect = OSError("disk full")
        return logger

    async def _bridge(_ws: object) -> None:
        bridged.set()
        await asyncio.Event().wait()

    _wire(monkeypatch, runtime, loggers, _make_logger, _bridge)
    monkeypatch.setattr(runtime, "_start_connector", AsyncMock(return_value=connector))
    runtime._connector = None
    runtime._state = "stopped"
    await runtime.start()
    await bridged.wait()

    with pytest.raises(OSError, match="disk full"):
        await runtime.stop()

    loggers[0].stop.assert_awaited_once()
    connector.stop.assert_awaited_once()
    assert runtime._connector is None
    assert runtime._pending_close_failure is None
    status = runtime.status()
    assert status.lifecycle_state == "stopped"
    assert status.last_error == "disk full"


async def test_a_run_task_failure_still_wins_over_a_pending_close_failure() -> None:
    runtime, _, connector = _runtime()
    runtime._pending_close_failure = OSError("disk full")

    async def _boom() -> None:
        raise RuntimeError("run loop died")

    runtime._task = asyncio.create_task(_boom())
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="run loop died"):
        await runtime.stop()

    _assert_down(runtime, connector)
    assert runtime._pending_close_failure is None


async def test_start_forgets_a_stale_pending_close_failure() -> None:
    runtime, _, _ = _runtime()
    runtime._pending_close_failure = OSError("disk full")
    runtime._state = "stopped"
    runtime._run = AsyncMock()  # type: ignore[method-assign]

    await runtime.start()

    assert runtime._pending_close_failure is None
    await runtime._task  # type: ignore[misc]
