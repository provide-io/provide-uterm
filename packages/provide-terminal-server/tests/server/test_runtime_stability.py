from unittest.mock import Mock

import pytest

from provide.terminal.server.models import RecordingConfig, SessionDefinition
from provide.terminal.server.runtime import HostedSessionRuntime


async def _get_next_message(runtime: HostedSessionRuntime) -> dict[str, object]:
    assert runtime._queue is not None
    return await runtime._queue.get()


@pytest.mark.asyncio
async def test_runtime_enforces_max_buffer_size():
    definition = SessionDefinition(
        session_id="s1", display_name="Test", connector_type="shell", connector_config={"command": "/bin/echo"}
    )
    recording = RecordingConfig(enabled_by_default=False)

    # Use a tiny buffer of 50 bytes for testing
    runtime = HostedSessionRuntime(
        definition, hub=Mock(), public_base_url="http://localhost", recording=recording, max_buffer_bytes=50
    )

    # Initialize queue
    await runtime.start()
    # We must stop the background task immediately so it doesn't drain the queue
    if runtime._task:
        runtime._task.cancel()

    # 1. Enqueue small message (approx 20 bytes when encoded as data)
    msg1 = {"type": "term", "data": "hello world"}
    await runtime._enqueue_messages([msg1])
    assert runtime._queue_bytes > 0
    assert runtime._queue.qsize() == 1
    assert await _get_next_message(runtime) == msg1
    assert runtime._queue.qsize() == 0

    # 2. Enqueue large message that exceeds remaining 50 bytes
    msg2 = {"type": "term", "data": "A" * 100}
    await runtime._enqueue_messages([msg2])

    # Should enqueue only the overflow error frame.
    assert runtime._queue.qsize() == 1
    err_msg = await _get_next_message(runtime)
    assert err_msg["type"] == "error"
    assert "Buffer overflow" in err_msg["message"]

    await runtime.stop()
