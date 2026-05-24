#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Browser → tunnel-worker input bridging.

The standard worker WS (``/ws/worker/{id}/term``) expects DLE-framed JSON
on the inbound side. Tunnel workers (``/tunnel/{id}``) speak the binary
tunnel-frame protocol and their bridge loop (e.g. ``uterm share``) writes
every received byte straight to PTY.

When a browser types into a tunnel-shared session, ``hub.send_worker``
must route the input as **raw bytes** (UTF-8 of the keystroke string),
not as a DLE-framed JSON envelope — otherwise the user's PTY sees
``{"type":"input",...}`` text.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.bridge.hub import TermHub
from provide.uterm.bridge.models import WorkerTermState


class _MockWs:
    """Minimal WS double — tracks send_bytes vs send_text calls."""

    def __init__(self) -> None:
        self.bytes_sent: list[bytes] = []
        self.text_sent: list[str] = []

    async def send_bytes(self, payload: bytes) -> None:
        self.bytes_sent.append(payload)

    async def send_text(self, payload: str) -> None:
        self.text_sent.append(payload)


@pytest.mark.asyncio
async def test_tunnel_worker_receives_raw_bytes_for_input() -> None:
    """``input`` to a tunnel worker should land as raw UTF-8 bytes."""
    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "operator")
    ws = _MockWs()
    # Inject the worker state directly to bypass the full register flow.
    hub._workers["tun-1"] = WorkerTermState(worker_ws=ws, is_tunnel_worker=True)  # type: ignore[attr-defined]

    ok = await hub.send_worker("tun-1", {"type": "input", "data": "ls -la\r"})

    assert ok is True
    assert ws.bytes_sent == [b"ls -la\r"]
    assert ws.text_sent == []


@pytest.mark.asyncio
async def test_regular_worker_still_receives_text_input() -> None:
    """Non-tunnel workers (``is_tunnel_worker=False``) keep the text-send path.

    ``_encode_worker_frame`` returns the raw input string (DLE-escaped) for
    ``type==input``, sent via ``send_text``. The tunnel path takes the same
    data but routes through ``send_bytes`` for the worker's PTY bridge.
    """
    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "operator")
    ws = _MockWs()
    hub._workers["std-1"] = WorkerTermState(worker_ws=ws, is_tunnel_worker=False)  # type: ignore[attr-defined]

    ok = await hub.send_worker("std-1", {"type": "input", "data": "echo hi\r"})

    assert ok is True
    assert ws.text_sent == ["echo hi\r"]
    assert ws.bytes_sent == []


@pytest.mark.asyncio
async def test_tunnel_worker_drops_non_input_messages() -> None:
    """Control frames to a tunnel worker should NOT reach the PTY.

    The existing ``uterm share`` bridge loop writes every received byte
    straight to PTY; if we sent a JSON-envelope ``snapshot_req`` the
    user would see ``{"type":"snapshot_req",...}`` in their terminal.
    """
    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "operator")
    ws = _MockWs()
    hub._workers["tun-2"] = WorkerTermState(worker_ws=ws, is_tunnel_worker=True)  # type: ignore[attr-defined]

    ok = await hub.send_worker("tun-2", {"type": "snapshot_req", "req_id": "x"})

    assert ok is True  # send is "successful" in the sense that it was handled
    assert ws.bytes_sent == []
    assert ws.text_sent == []


@pytest.mark.asyncio
async def test_tunnel_worker_input_with_non_string_data_dropped() -> None:
    """Defensive: malformed input with non-string data shouldn't crash."""
    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "operator")
    ws = _MockWs()
    hub._workers["tun-3"] = WorkerTermState(worker_ws=ws, is_tunnel_worker=True)  # type: ignore[attr-defined]

    ok = await hub.send_worker("tun-3", {"type": "input", "data": 12345})

    assert ok is True
    assert ws.bytes_sent == []


@pytest.mark.asyncio
async def test_set_worker_tunnel_flag_writes_through() -> None:
    """``set_worker_tunnel_flag`` mutates the state under the lock."""
    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "operator")
    hub._workers["w1"] = WorkerTermState(worker_ws=_MockWs(), is_tunnel_worker=False)  # type: ignore[attr-defined]

    await hub.set_worker_tunnel_flag("w1", True)
    assert hub._workers["w1"].is_tunnel_worker is True  # type: ignore[attr-defined]

    await hub.set_worker_tunnel_flag("w1", False)
    assert hub._workers["w1"].is_tunnel_worker is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_set_worker_tunnel_flag_unknown_worker_is_noop() -> None:
    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "operator")
    # Should not raise.
    await hub.set_worker_tunnel_flag("does-not-exist", True)


@pytest.mark.asyncio
async def test_tunnel_worker_send_returns_false_when_no_worker() -> None:
    """No worker registered → returns False even with the tunnel flag in play."""
    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "operator")
    ok = await hub.send_worker("absent", {"type": "input", "data": "x"})
    assert ok is False


@pytest.mark.asyncio
async def test_tunnel_worker_send_handles_exception() -> None:
    """If ws.send_bytes raises, worker_ws gets cleared and send returns False."""
    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "operator")
    ws: Any = MagicMock()
    ws.send_bytes = AsyncMock(side_effect=ConnectionError("ws broke"))
    hub._workers["tun-err"] = WorkerTermState(worker_ws=ws, is_tunnel_worker=True)  # type: ignore[attr-defined]

    ok = await hub.send_worker("tun-err", {"type": "input", "data": "ping\r"})

    assert ok is False
    assert hub._workers["tun-err"].worker_ws is None  # type: ignore[attr-defined]
