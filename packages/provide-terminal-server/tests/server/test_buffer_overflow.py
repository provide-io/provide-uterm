#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import asyncio
from types import SimpleNamespace

import pytest

from provide.terminal.server.models import RecordingConfig, SessionDefinition
from provide.terminal.server.runtime import HostedSessionRuntime


def _make_session(session_id: str = "test-session", connector_type: str = "shell") -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        display_name="Test Session",
        connector_type=connector_type,
        auto_start=False,
    )


def _make_runtime(
    session_id: str = "test-session",
    base_url: str = "http://localhost:9999",
) -> HostedSessionRuntime:
    rt = HostedSessionRuntime(
        _make_session(session_id),
        public_base_url=base_url,
        recording=RecordingConfig(),
        hub=SimpleNamespace(metric=lambda _name: None),
    )
    rt._max_buffer_bytes = 100  # Small buffer for testing
    return rt


async def _get_next_message(rt: HostedSessionRuntime) -> dict[str, object]:
    assert rt._queue is not None
    return await rt._queue.get()


@pytest.mark.asyncio
async def test_enqueue_messages_buffer_overflow_emits_error():
    rt = _make_runtime()
    rt._queue = asyncio.Queue()

    # Message that fits
    msg1 = {"type": "term", "data": "A" * 50}
    await rt._enqueue_messages([msg1])
    assert rt._queue.qsize() == 1
    assert await _get_next_message(rt) == msg1
    assert rt._queue.qsize() == 0

    # Message that overflows
    msg2 = {"type": "term", "data": "B" * 60}
    await rt._enqueue_messages([msg2])

    # Queue should contain only the overflow error frame.
    assert rt._queue.qsize() == 1
    err_msg = await _get_next_message(rt)
    assert err_msg == {"type": "error", "message": "Buffer overflow — input dropped"}
