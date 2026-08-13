#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``recording.flush_*`` must reach the SessionLogger that acts on it.

Both knobs were validated on load and then dropped on the floor: the only
production ``SessionLogger`` construction site (``runtime._start_connector``)
never forwarded them.  Two things hid it — the constructor default
(``flush_interval_s=5.0``) is identical to the config default, so nothing looked
wrong at runtime, and the config spells the batch knob ``flush_batch_size``
while the constructor parameter is ``batch_size``, so no name-based grep or
``**dump`` splat would have caught the omission.

Every assertion here is on observable flush *behaviour* (what the recording
store received), never on the logger's attributes: an
``assert logger._batch_size == 2`` wiring check is exactly the shape of test
that let the bug survive, since the attribute is set correctly from a default
the caller never overrode.

Two layers are covered on purpose:

* ``TestRuntimeHonoursFlushConfig`` — the runtime honours the RecordingConfig
  it is handed.  This pins the fix itself.
* ``TestHostedConfigReachesLogger`` — an operator's config *mapping* survives
  ``config_from_mapping`` -> ``create_server_app`` -> ``SessionRegistry`` ->
  runtime.  This is the layer that would actually have caught the original bug,
  because the runtime-level default made the unit layer look healthy.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from provide.uterm.recording import InMemoryRecordingStore
from provide.uterm.server.config import config_from_mapping
from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime


class _BatchSpyStore(InMemoryRecordingStore):
    """In-memory store that remembers each ``append_events`` batch it received."""

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[dict[str, Any]]] = []

    async def append_events(self, session_id: str, events: list[dict[str, Any]]) -> None:
        self.batches.append(list(events))
        await super().append_events(session_id, events)


def _make_definition(**kwargs: Any) -> SessionDefinition:
    defaults: dict[str, Any] = {
        "session_id": "flush-session",
        "display_name": "Flush Session",
        "connector_type": "shell",
        "auto_start": False,
        "recording_enabled": True,
    }
    defaults.update(kwargs)
    return SessionDefinition(**defaults)


def _make_connector() -> MagicMock:
    connector = MagicMock()
    connector.is_connected = MagicMock(return_value=False)
    connector.start = AsyncMock()
    connector.stop = AsyncMock()
    connector.get_snapshot = AsyncMock(return_value={"type": "snapshot", "screen": "test", "ts": 1.0})
    connector.get_analysis = AsyncMock(return_value="analysis text")
    connector.poll_messages = AsyncMock(return_value=[])
    connector.set_mode = AsyncMock(return_value=[])
    connector.handle_control = AsyncMock(return_value=[])
    connector.handle_input = AsyncMock(return_value=[])
    return connector


async def _wait_for_batch(store: _BatchSpyStore, deadline_s: float = 2.0) -> bool:
    """Poll until the store receives a batch, bounded so a red run stays fast."""
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline_s
    while loop.time() < end:
        if store.batches:
            return True
        await asyncio.sleep(0.01)
    return False


class TestRuntimeHonoursFlushConfig:
    async def test_flush_batch_size_triggers_flush_at_configured_count(self) -> None:
        """Two events with ``flush_batch_size=2`` must reach the store immediately."""
        store = _BatchSpyStore()
        runtime = HostedSessionRuntime(
            _make_definition(),
            public_base_url="http://localhost:9999",
            # Interval left at its default so the only thing that can flush
            # within the test is the batch threshold.
            recording=RecordingConfig(flush_batch_size=2),
            recording_store=store,
        )

        with patch("provide.uterm.server.runtime.build_connector", return_value=_make_connector()):
            runtime._connector = await runtime._start_connector()
            await runtime._start_recording()
        try:
            await runtime._log_event("first", {})
            await runtime._log_event("second", {})

            assert store.batches, "flush_batch_size=2 did not flush after 2 events"
            assert [e["event"] for e in store.batches[0]] == ["first", "second"]
        finally:
            await runtime._stop_connector()

    async def test_flush_interval_flushes_below_the_batch_threshold(self) -> None:
        """A short ``flush_interval_s`` must flush a single event on its own."""
        store = _BatchSpyStore()
        runtime = HostedSessionRuntime(
            _make_definition(),
            public_base_url="http://localhost:9999",
            # Batch size left at its default (100) so only the periodic
            # flusher can move this single event.
            recording=RecordingConfig(flush_interval_s=0.02),
            recording_store=store,
        )

        with patch("provide.uterm.server.runtime.build_connector", return_value=_make_connector()):
            runtime._connector = await runtime._start_connector()
            await runtime._start_recording()
        try:
            await runtime._log_event("lonely", {})

            assert await _wait_for_batch(store), "flush_interval_s=0.02 never flushed the buffered event"
            assert any(e["event"] == "lonely" for batch in store.batches for e in batch)
        finally:
            await runtime._stop_connector()


class TestHostedConfigReachesLogger:
    async def test_operator_config_flush_batch_size_reaches_the_logger(self) -> None:
        """A flush knob set in an operator's config survives the whole boot path."""
        store = _BatchSpyStore()
        config = config_from_mapping(
            {
                "recording": {
                    "enabled_by_default": True,
                    "store_type": "memory",
                    "flush_batch_size": 2,
                },
                "sessions": [
                    {
                        "session_id": "hosted-flush",
                        "display_name": "Hosted Flush",
                        "connector_type": "shell",
                        "auto_start": False,
                    }
                ],
            }
        )

        # The only injected seam: the store is swapped for a spy so the batches
        # are observable.  Everything between the config mapping and the logger
        # — factory, registry, runtime — is the real production path.
        from provide.uterm.server.app import create_server_app

        with patch("provide.uterm.server.app.factory_impl.build_recording_store", return_value=store):
            app = create_server_app(config, api_only=True)
        registry = app.state.uterm_registry

        status = await registry.get_session("hosted-flush")
        runtime = registry.get_runtime(status.session_id)
        assert runtime is not None

        with patch("provide.uterm.server.runtime.build_connector", return_value=_make_connector()):
            runtime._connector = await runtime._start_connector()
            await runtime._start_recording()
        try:
            await runtime._log_event("first", {})
            await runtime._log_event("second", {})

            assert store.batches, "recording.flush_batch_size=2 from config never reached the logger"
            assert [e["event"] for e in store.batches[0]] == ["first", "second"]
        finally:
            await runtime._stop_connector()
