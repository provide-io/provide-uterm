import pytest
from unittest.mock import Mock, AsyncMock
from provide.terminal.server.runtime import HostedSessionRuntime
from provide.terminal.server.models import SessionDefinition, RecordingConfig

@pytest.mark.asyncio
async def test_runtime_enforces_max_buffer_size():
    definition = SessionDefinition(
        session_id="s1",
        display_name="Test",
        connector_type="shell",
        connector_config={"command": "/bin/echo"}
    )
    recording = RecordingConfig(enabled_by_default=False)
    
    # Use a tiny buffer of 50 bytes for testing
    runtime = HostedSessionRuntime(
        definition,
        hub=Mock(),
        public_base_url="http://localhost",
        recording=recording,
        max_buffer_bytes=50
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
    
    # 2. Enqueue large message that exceeds remaining 50 bytes
    msg2 = {"type": "term", "data": "A" * 100}
    await runtime._enqueue_messages([msg2])
    
    # Should be 2 (msg2 dropped, but error message enqueued)
    assert runtime._queue.qsize() == 2
    last_msg = runtime._queue._queue[-1]
    assert last_msg["type"] == "error"
    assert "Buffer overflow" in last_msg["message"]
    
    await runtime.stop()
