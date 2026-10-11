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
from unittest.mock import AsyncMock

import pytest

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
