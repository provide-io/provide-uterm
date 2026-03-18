#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for server/runtime.py fixes.

Covers:
- asyncio.Queue created with maxsize=2000 (not unbounded).
"""

from __future__ import annotations

import pytest

from provide.terminal.server.models import RecordingConfig, SessionDefinition
from provide.terminal.server.runtime import HostedSessionRuntime


def _make_runtime() -> HostedSessionRuntime:
    defn = SessionDefinition(session_id="test-session", connector_type="shell")
    return HostedSessionRuntime(
        defn,
        public_base_url="http://localhost:8780",
        recording=RecordingConfig(),
    )


class TestQueueMaxsize:
    async def test_queue_has_maxsize_after_start(self) -> None:
        """Queue must be created with maxsize=2000 in start().

        Kills the mutation:
          asyncio.Queue(maxsize=2000) → asyncio.Queue()
        An unbounded queue allows memory exhaustion when the connector floods
        faster than WS throughput.
        """
        rt = _make_runtime()
        # Queue is None before start()
        assert rt._queue is None

        # Patch _run so it doesn't actually try to connect
        rt._task = None

        import asyncio

        original_create_task = asyncio.create_task

        async def _dummy_run() -> None:
            await asyncio.sleep(0)

        tasks_created: list[asyncio.Task[None]] = []

        def _fake_create_task(coro):  # type: ignore[no-untyped-def]
            task = original_create_task(coro)
            tasks_created.append(task)
            return task

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("asyncio.create_task", _fake_create_task)
            # Replace _run to avoid real WS connection
            rt._run = _dummy_run  # type: ignore[method-assign]
            await rt.start()

        assert rt._queue is not None, "Queue must be set after start()"
        assert rt._queue.maxsize == 2000, (
            f"Queue maxsize must be 2000, got {rt._queue.maxsize} — unbounded queue allows memory exhaustion"
        )

        await rt.stop()

    async def test_queue_is_bounded_not_infinite(self) -> None:
        """Queue maxsize must be > 0 (not the default unbounded 0).

        An asyncio.Queue(maxsize=0) means unlimited capacity.
        asyncio.Queue() also defaults to maxsize=0.
        """
        rt = _make_runtime()

        async def _dummy_run() -> None:
            import asyncio

            await asyncio.sleep(0)

        rt._run = _dummy_run  # type: ignore[method-assign]
        await rt.start()

        assert rt._queue is not None
        # maxsize=0 means unbounded in asyncio.Queue
        assert rt._queue.maxsize > 0, "Queue maxsize=0 means unbounded — must be a positive integer"

        await rt.stop()


class TestLastErrorResetOnStart:
    async def test_last_error_is_none_after_start(self) -> None:
        """start() must reset _last_error to None (not "" or any other value).

        Kills mutmut_11: self._last_error = None → self._last_error = ""
        An empty string is truthy-equivalent but is_not None — callers that check
        `if last_error is None` to distinguish "no error" from "error present"
        would misinterpret "" as an error message.
        """
        import asyncio

        rt = _make_runtime()
        rt._last_error = "previous error"  # simulate a past failure

        async def _dummy_run() -> None:
            await asyncio.sleep(0)

        rt._run = _dummy_run  # type: ignore[method-assign]
        await rt.start()

        assert rt._last_error is None, (
            "start() must set _last_error = None to clear any previous error; "
            "'' would falsely look like an error string to identity checks"
        )

        await rt.stop()
