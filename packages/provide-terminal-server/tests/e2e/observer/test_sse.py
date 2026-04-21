#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E: SSE streaming endpoint via live uvicorn server.

Scenarios
---------
1. Shell session worker sends a snapshot → SSE client receives it.
2. Telnet session worker sends a snapshot → SSE client receives it.
3. Worker disconnect → SSE stream delivers worker_disconnected event then closes.
4. Pattern filter via SSE — non-matching events dropped, matching delivered.
5. event_types filter — only specified types reach SSE subscriber.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from provide.terminal.client import connect_async_ws
from tests.e2e._live_server import live_server_with_bus

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def shell_server() -> Any:
    sessions = [{"session_id": "sse1", "display_name": "SSE Shell", "connector_type": "shell", "auto_start": False}]
    async with live_server_with_bus(sessions, label="sse_shell") as result:
        yield result


@pytest.fixture()
async def multi_session_server() -> Any:
    sessions = [
        {"session_id": "sse-shell", "display_name": "SSE Shell", "connector_type": "shell", "auto_start": False},
        {"session_id": "sse-telnet", "display_name": "SSE Telnet", "connector_type": "shell", "auto_start": False},
    ]
    async with live_server_with_bus(sessions, label="sse_multi") as result:
        yield result


def ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


def snapshot_msg(screen: str = "$ test") -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": "sse-hash",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": "sse-p"},
        "ts": time.time(),
    }


async def collect_sse_events(
    base_url: str,
    session_id: str,
    *,
    max_events: int = 1,
    timeout_s: float = 6.0,
    event_types: str | None = None,
    pattern: str | None = None,
) -> list[dict[str, Any]]:
    """Connect to SSE stream and collect up to *max_events* events."""
    params: dict[str, Any] = {}
    if event_types:
        params["event_types"] = event_types
    if pattern:
        params["pattern"] = pattern

    events: list[dict[str, Any]] = []
    async with (
        httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=timeout_s + 2) as http,
        http.stream("GET", f"/api/sessions/{session_id}/events/stream", params=params) as resp,
    ):
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                events.append(payload)
                if len(events) >= max_events:
                    break
    return events


# ---------------------------------------------------------------------------
# 1. Shell session snapshot reaches SSE subscriber
# ---------------------------------------------------------------------------


async def test_sse_shell_snapshot_received(shell_server: Any) -> None:
    hub, base_url = shell_server

    async with connect_async_ws(ws_url(base_url, "/ws/worker/sse1/term")) as worker:
        await worker.recv()  # snapshot_req

        collect_task = asyncio.create_task(collect_sse_events(base_url, "sse1", max_events=1, timeout_s=8.0))
        await asyncio.sleep(0.15)

        await worker.send(json.dumps(snapshot_msg("$ shell sse test")))
        events = await asyncio.wait_for(collect_task, timeout=10.0)

    assert len(events) == 1
    assert events[0]["type"] == "snapshot"
    assert events[0]["worker_id"] == "sse1"


# ---------------------------------------------------------------------------
# 2. Two session types (shell×2) are isolated in SSE
# ---------------------------------------------------------------------------


async def test_sse_two_sessions_isolated(multi_session_server: Any) -> None:
    """SSE subscriber for sse-shell only receives sse-shell events."""
    hub, base_url = multi_session_server

    async with (
        connect_async_ws(ws_url(base_url, "/ws/worker/sse-shell/term")) as w_shell,
        connect_async_ws(ws_url(base_url, "/ws/worker/sse-telnet/term")) as w_telnet,
    ):
        await w_shell.recv()
        await w_telnet.recv()

        collect_task = asyncio.create_task(collect_sse_events(base_url, "sse-shell", max_events=1, timeout_s=8.0))
        await asyncio.sleep(0.15)

        # Fire from telnet session first — should NOT reach sse-shell SSE
        await w_telnet.send(json.dumps(snapshot_msg("$ telnet event")))
        await asyncio.sleep(0.15)
        assert not collect_task.done()

        # Now fire from shell session
        await w_shell.send(json.dumps(snapshot_msg("$ shell event")))
        events = await asyncio.wait_for(collect_task, timeout=8.0)

    assert len(events) == 1
    assert events[0]["worker_id"] == "sse-shell"


# ---------------------------------------------------------------------------
# 3. Worker disconnect → worker_disconnected event, stream closes
# ---------------------------------------------------------------------------


async def test_sse_worker_disconnect_closes_stream(shell_server: Any) -> None:
    hub, base_url = shell_server

    async with connect_async_ws(ws_url(base_url, "/ws/worker/sse1/term")) as worker:
        await worker.recv()

        events: list[dict[str, Any]] = []

        async def _stream() -> None:
            async with (
                httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=12) as http,
                http.stream("GET", "/api/sessions/sse1/events/stream") as resp,
            ):
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))

        stream_task = asyncio.create_task(_stream())
        await asyncio.sleep(0.15)

    # Worker disconnected (context exited) — stream should have closed
    await asyncio.wait_for(stream_task, timeout=5.0)
    assert any(e.get("type") == "worker_disconnected" for e in events)


# ---------------------------------------------------------------------------
# 4. Pattern filter via SSE
# ---------------------------------------------------------------------------


async def test_sse_pattern_filter(shell_server: Any) -> None:
    hub, base_url = shell_server

    async with connect_async_ws(ws_url(base_url, "/ws/worker/sse1/term")) as worker:
        await worker.recv()

        collect_task = asyncio.create_task(
            collect_sse_events(
                base_url,
                "sse1",
                max_events=1,
                timeout_s=8.0,
                event_types="snapshot",
                pattern=r"\$ ",
            )
        )
        await asyncio.sleep(0.15)

        # Non-matching — filtered
        await worker.send(json.dumps(snapshot_msg("loading...")))
        await asyncio.sleep(0.15)
        assert not collect_task.done()

        # Matching
        await worker.send(json.dumps(snapshot_msg("root@host:~$ ")))
        events = await asyncio.wait_for(collect_task, timeout=8.0)

    assert len(events) == 1
    assert events[0]["type"] == "snapshot"


# ---------------------------------------------------------------------------
# 5. event_types filter via SSE
# ---------------------------------------------------------------------------


async def test_sse_event_types_filter(shell_server: Any) -> None:
    hub, base_url = shell_server

    async with connect_async_ws(ws_url(base_url, "/ws/worker/sse1/term")) as worker:
        await worker.recv()

        collect_task = asyncio.create_task(
            collect_sse_events(
                base_url,
                "sse1",
                max_events=1,
                timeout_s=8.0,
                event_types="hijack_acquired",
            )
        )
        await asyncio.sleep(0.15)

        # snapshot should be filtered
        await worker.send(json.dumps(snapshot_msg("$ x")))
        await asyncio.sleep(0.15)
        assert not collect_task.done()

        # inject hijack_acquired directly
        event_bus = hub.event_bus
        assert event_bus is not None
        event_bus._enqueue(  # type: ignore[attr-defined]
            "sse1",
            {"type": "hijack_acquired", "seq": 99, "ts": time.time(), "data": {}},
        )
        events = await asyncio.wait_for(collect_task, timeout=8.0)

    assert len(events) == 1
    assert events[0]["type"] == "hijack_acquired"
