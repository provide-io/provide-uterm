#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for SessionRegistry.stream_session_events() SSE generator."""

from __future__ import annotations

import asyncio
import json

from provide.uterm.bridge.hub import EventBus, TermHub
from provide.uterm.server.config import config_from_mapping
from provide.uterm.server.models import RecordingConfig
from provide.uterm.server.registry import SessionRegistry


def _make_registry(with_bus: bool = True) -> tuple[SessionRegistry, TermHub, EventBus | None]:
    bus = EventBus() if with_bus else None
    hub = TermHub(event_bus=bus)
    registry = SessionRegistry(
        [],
        hub=hub,
        public_base_url="http://localhost:8780",
        recording=RecordingConfig(),
    )
    return registry, hub, bus


# ---------------------------------------------------------------------------
# No EventBus → empty generator
# ---------------------------------------------------------------------------


async def test_stream_no_event_bus_returns_empty() -> None:
    registry, _hub, _bus = _make_registry(with_bus=False)
    chunks = [chunk async for chunk in registry.stream_session_events("w1")]
    assert chunks == []


# ---------------------------------------------------------------------------
# Events are yielded as SSE data lines
# ---------------------------------------------------------------------------


async def test_stream_yields_sse_formatted_event() -> None:
    registry, hub, bus = _make_registry()
    assert bus is not None
    await hub._get("w1")

    results: list[str] = []

    async def _collect() -> None:
        async for chunk in registry.stream_session_events("w1"):
            results.append(chunk)
            if chunk.strip():  # stop after first real event
                break

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0.05)
    await hub.append_event("w1", "snapshot", {"screen": "hello"})
    await asyncio.wait_for(task, timeout=2.0)

    assert len(results) == 1
    parsed = json.loads(results[0].removeprefix("data: ").rstrip())
    assert parsed["type"] == "snapshot"
    assert parsed["worker_id"] == "w1"


# ---------------------------------------------------------------------------
# Worker disconnect sentinel → yields worker_disconnected then stops
# ---------------------------------------------------------------------------


async def test_stream_sentinel_yields_worker_disconnected_and_stops() -> None:
    registry, hub, bus = _make_registry()
    assert bus is not None
    await hub._get("w1")

    results: list[str] = []

    async def _collect() -> None:
        async for chunk in registry.stream_session_events("w1"):
            results.append(chunk)

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0.05)
    # Trigger sentinel by closing the worker
    bus.close_worker("w1")
    await asyncio.wait_for(task, timeout=2.0)

    assert len(results) == 1
    assert '"type":"worker_disconnected"' in results[0]


# ---------------------------------------------------------------------------
# Heartbeat emitted on idle
# ---------------------------------------------------------------------------


async def test_stream_emits_heartbeat_on_idle() -> None:
    registry, hub, bus = _make_registry()
    assert bus is not None
    await hub._get("w1")

    results: list[str] = []

    async def _collect() -> None:
        async for chunk in registry.stream_session_events("w1", heartbeat_s=0.05):
            results.append(chunk)
            if len(results) >= 2:
                break

    task = asyncio.create_task(_collect())
    # Don't send any events — heartbeats should fire naturally
    await asyncio.wait_for(task, timeout=2.0)

    assert all('"type":"heartbeat"' in r for r in results)


# ---------------------------------------------------------------------------
# event_types filter is respected
# ---------------------------------------------------------------------------


async def test_stream_event_types_filter() -> None:
    registry, hub, bus = _make_registry()
    assert bus is not None
    await hub._get("w1")

    results: list[str] = []

    async def _collect() -> None:
        async for chunk in registry.stream_session_events("w1", event_types=["hijack_acquired"]):
            results.append(chunk)
            break

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0.05)

    # Send snapshot — should be filtered out
    await hub.append_event("w1", "snapshot", {"screen": "x"})
    await asyncio.sleep(0.05)
    assert results == []  # nothing yet

    # Send hijack_acquired — should pass through
    await hub.append_event("w1", "hijack_acquired", {"hijack_id": "abc"})
    await asyncio.wait_for(task, timeout=2.0)

    assert len(results) == 1
    parsed = json.loads(results[0].removeprefix("data: ").rstrip())
    assert parsed["type"] == "hijack_acquired"


# ---------------------------------------------------------------------------
# pattern filter is respected
# ---------------------------------------------------------------------------


async def test_stream_pattern_filter() -> None:
    registry, hub, bus = _make_registry()
    assert bus is not None
    await hub._get("w1")

    results: list[str] = []

    async def _collect() -> None:
        async for chunk in registry.stream_session_events("w1", event_types=["snapshot"], pattern=r"\$ "):
            results.append(chunk)
            break

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0.05)

    # Non-matching snapshot — filtered
    await hub.append_event("w1", "snapshot", {"screen": "loading..."})
    await asyncio.sleep(0.05)
    assert results == []

    # Matching snapshot — passes
    await hub.append_event("w1", "snapshot", {"screen": "root@host:~$ "})
    await asyncio.wait_for(task, timeout=2.0)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# SSE route: 404 for unknown session, 403 for insufficient privileges
# ---------------------------------------------------------------------------


async def test_stream_route_404_unknown_session() -> None:
    from fastapi.testclient import TestClient

    from provide.uterm.server.app import create_server_app

    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 8780},
            "auth": {
                "mode": "header",
                "header_mode_acknowledged": True,
                "worker_bearer_token": "test-bearer-token-32-chars-long-x",
            },
            "sessions": [],
        }
    )
    app = create_server_app(cfg)

    with TestClient(app) as client:
        resp = client.get(
            "/api/sessions/no-such/events/stream",
            headers={"X-Uterm-Principal": "user1", "X-Uterm-Role": "admin"},
        )
    assert resp.status_code == 404


async def test_stream_route_403_insufficient_privileges() -> None:
    from fastapi.testclient import TestClient

    from provide.uterm.server.app import create_server_app

    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 8780},
            "auth": {
                "mode": "header",
                "header_mode_acknowledged": True,
                "worker_bearer_token": "test-bearer-token-32-chars-long-x",
            },
            "sessions": [
                {"session_id": "s1", "display_name": "S1", "connector_type": "shell", "visibility": "private"}
            ],
        }
    )
    app = create_server_app(cfg)

    with TestClient(app) as client:
        resp = client.get(
            "/api/sessions/s1/events/stream",
            headers={"X-Uterm-Principal": "user1", "X-Uterm-Role": "viewer"},
        )
    # viewer cannot read private session → 403
    assert resp.status_code == 403


async def test_stream_route_no_bus_returns_empty_stream() -> None:
    """No EventBus → stream closes immediately with no data.

    ``create_server_app`` always wires an EventBus onto the hub, so we patch
    the hub's ``event_bus`` attribute to None to simulate the legacy no-bus
    deployment path.  Without this, the stream would block for 15s on a
    heartbeat and hang the test.
    """
    from fastapi.testclient import TestClient

    from provide.uterm.server.app import create_server_app

    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 8780},
            "auth": {
                "mode": "header",
                "header_mode_acknowledged": True,
                "worker_bearer_token": "test-bearer-token-32-chars-long-x",
            },
            "sessions": [{"session_id": "s1", "display_name": "S1", "connector_type": "shell", "auto_start": False}],
        }
    )
    app = create_server_app(cfg)
    # Unbind the EventBus so stream_session_events hits its early-return branch.
    app.state.uterm_registry._hub._event_bus = None
    with (
        TestClient(app) as client,
        client.stream(
            "GET",
            "/api/sessions/s1/events/stream",
            headers={"X-Uterm-Principal": "user1", "X-Uterm-Role": "admin"},
        ) as resp,
    ):
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/event-stream; charset=utf-8"
        body = b"".join(resp.iter_bytes())
    assert body == b""
