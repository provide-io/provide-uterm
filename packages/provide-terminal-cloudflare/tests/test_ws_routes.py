#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for api/ws_routes.py — handle_socket_message dispatch."""

from __future__ import annotations

from types import SimpleNamespace

from provide.terminal.cloudflare.api.ws_routes import handle_socket_message
from provide.terminal.cloudflare.bridge.hijack import HijackCoordinator
from provide.terminal.cloudflare.contracts import frame_json

# ---------------------------------------------------------------------------
# Minimal runtime mock
# ---------------------------------------------------------------------------


class _Runtime:
    def __init__(self, *, input_mode: str = "hijack", browser_role: str = "admin") -> None:
        self.worker_id = "w1"
        self.meta: dict = {
            "display_name": self.worker_id,
            "connector_type": "unknown",
            "created_at": 0.0,
            "tags": [],
            "visibility": "public",
            "owner": None,
        }
        self.lifecycle_state = "stopped"
        self.input_mode = input_mode
        self.hijack = HijackCoordinator()
        self.last_snapshot: dict | None = None
        self.last_analysis: str | None = None
        self.browser_hijack_owner: dict[str, str] = {}
        self._browser_role = browser_role
        self._sent: list[dict] = []
        self._pushed: list[str] = []
        self._broadcast: list[dict] = []
        self._snapshots_saved: list[dict] = []
        self._input_modes_saved: list[str] = []
        self.config = SimpleNamespace(limits=SimpleNamespace(max_ws_message_bytes=1_048_576, max_input_chars=10_000))
        self.store = SimpleNamespace(
            save_snapshot=lambda wid, snap: self._snapshots_saved.append(snap),
            save_input_mode=lambda wid, mode: self._input_modes_saved.append(mode),
        )

    async def send_ws(self, ws: object, frame: dict) -> None:
        self._sent.append(frame)

    async def push_worker_input(self, data: str) -> bool:
        self._pushed.append(data)
        return True

    async def broadcast_worker_frame(self, frame: object) -> None:
        self._broadcast.append(frame)

    def ws_key(self, ws: object) -> str:
        return str(id(ws))

    def _socket_browser_role(self, ws: object) -> str:
        return self._browser_role


class _Ws:
    pass


def _raw(frame_type: str, **kwargs) -> str:
    return frame_json(frame_type, **kwargs)


# ---------------------------------------------------------------------------
# ProtocolError handling
# ---------------------------------------------------------------------------


async def test_protocol_error_sends_error_frame() -> None:
    """Malformed control stream → ProtocolError → error frame sent to ws."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, "\x10X", is_worker=False)
    assert runtime._sent
    assert runtime._sent[0]["type"] == "error"


async def test_message_too_large_sends_error() -> None:
    """Oversized message → ProtocolError → error frame."""
    runtime = _Runtime()
    runtime.config.limits.max_ws_message_bytes = 10
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("input", data="x" * 100), is_worker=False)
    assert runtime._sent[0]["type"] == "error"


# ---------------------------------------------------------------------------
# Worker frames
# ---------------------------------------------------------------------------


async def test_worker_snapshot_frame_saves_snapshot() -> None:
    """snapshot frame from worker: saves to store and sets last_snapshot."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("snapshot", screen="hello"), is_worker=True)
    assert runtime.last_snapshot is not None
    assert runtime.last_snapshot["screen"] == "hello"
    assert runtime._snapshots_saved


async def test_worker_snapshot_broadcasts() -> None:
    """snapshot frame from worker: broadcast_worker_frame called."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("snapshot", screen="x"), is_worker=True)
    assert runtime._broadcast


async def test_worker_hello_hijack_mode_sets_input_mode() -> None:
    """worker_hello with input_mode=hijack: runtime.input_mode updated."""
    runtime = _Runtime(input_mode="open")
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("worker_hello", input_mode="hijack"), is_worker=True)
    assert runtime.input_mode == "hijack"
    assert "hijack" in runtime._input_modes_saved


async def test_worker_hello_open_mode_sets_input_mode() -> None:
    """worker_hello with input_mode=open (no active hijack): accepted."""
    runtime = _Runtime(input_mode="hijack")
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("worker_hello", input_mode="open"), is_worker=True)
    assert runtime.input_mode == "open"


async def test_worker_hello_open_blocked_when_hijack_active() -> None:
    """worker_hello with input_mode=open while hijack is active: blocked."""
    runtime = _Runtime()
    runtime.hijack.acquire("alice", lease_s=60)
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("worker_hello", input_mode="open"), is_worker=True)
    # input_mode must NOT have changed to "open"
    assert runtime.input_mode == "hijack"
    assert "open" not in runtime._input_modes_saved


async def test_worker_hello_invalid_mode_ignored() -> None:
    """worker_hello with unsupported mode: no change."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("worker_hello", input_mode="bogus"), is_worker=True)
    assert runtime.input_mode == "hijack"
    assert not runtime._input_modes_saved


async def test_worker_non_special_frame_broadcasts() -> None:
    """Non-snapshot/worker_hello frame from worker: broadcast_worker_frame called."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("term", data="output"), is_worker=True)
    assert runtime._broadcast


# ---------------------------------------------------------------------------
# Browser frames — open mode
# ---------------------------------------------------------------------------


async def test_browser_input_open_mode_admin_sent() -> None:
    """Open mode + admin role: input forwarded to worker."""
    runtime = _Runtime(input_mode="open", browser_role="admin")
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("input", data="ls\r"), is_worker=False)
    assert runtime._pushed == ["ls\r"]


async def test_browser_input_open_mode_operator_sent() -> None:
    """Open mode + operator role: input forwarded."""
    runtime = _Runtime(input_mode="open", browser_role="operator")
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("input", data="pwd\r"), is_worker=False)
    assert runtime._pushed == ["pwd\r"]


async def test_browser_input_open_mode_viewer_blocked() -> None:
    """Open mode + viewer role: viewer_cannot_send error sent."""
    runtime = _Runtime(input_mode="open", browser_role="viewer")
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("input", data="ls\r"), is_worker=False)
    assert not runtime._pushed
    assert runtime._sent[0]["message"] == "viewer_cannot_send"


# ---------------------------------------------------------------------------
# Browser frames — hijack mode
# ---------------------------------------------------------------------------


async def test_browser_input_hijack_mode_no_session_error() -> None:
    """Hijack mode with no active session: not_hijacked error."""
    runtime = _Runtime(input_mode="hijack")
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("input", data="ls\r"), is_worker=False)
    assert runtime._sent[0]["message"] == "not_hijacked"


async def test_browser_input_hijack_mode_wrong_owner_error() -> None:
    """Hijack mode, active session, browser not the owner: not_owner error."""
    runtime = _Runtime(input_mode="hijack")
    runtime.hijack.acquire("alice", lease_s=60)
    ws = _Ws()
    # browser_hijack_owner is empty (this ws doesn't own the hijack)
    await handle_socket_message(runtime, ws, _raw("input", data="ls\r"), is_worker=False)
    assert runtime._sent[0]["message"] == "not_owner"


async def test_browser_input_hijack_mode_owner_sent() -> None:
    """Hijack mode, correct owner: input forwarded."""
    runtime = _Runtime(input_mode="hijack")
    result = runtime.hijack.acquire("alice", lease_s=60)
    ws = _Ws()
    runtime.browser_hijack_owner[runtime.ws_key(ws)] = result.session.hijack_id
    await handle_socket_message(runtime, ws, _raw("input", data="ls\r"), is_worker=False)
    assert runtime._pushed == ["ls\r"]


# ---------------------------------------------------------------------------
# Browser frames — REST-only hijack frames
# ---------------------------------------------------------------------------


async def test_browser_hijack_request_rejected() -> None:
    """hijack_request from browser: use_rest_hijack_api error sent."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("hijack_request"), is_worker=False)
    assert runtime._sent[0]["message"] == "use_rest_hijack_api"


async def test_browser_hijack_release_rejected() -> None:
    """hijack_release from browser: use_rest_hijack_api error sent."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("hijack_release"), is_worker=False)
    assert runtime._sent[0]["message"] == "use_rest_hijack_api"


async def test_browser_hijack_step_rejected() -> None:
    """hijack_step from browser: use_rest_hijack_api error sent."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("hijack_step"), is_worker=False)
    assert runtime._sent[0]["message"] == "use_rest_hijack_api"


# ---------------------------------------------------------------------------
# Browser frames — passthrough (no response)
# ---------------------------------------------------------------------------


async def test_browser_heartbeat_no_response() -> None:
    """heartbeat: no error sent, no input pushed."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("heartbeat"), is_worker=False)
    assert not runtime._sent
    assert not runtime._pushed


async def test_browser_ping_no_response() -> None:
    """ping: no error sent."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("ping"), is_worker=False)
    assert not runtime._sent


# ---------------------------------------------------------------------------
# Worker frames — analysis
# ---------------------------------------------------------------------------


async def test_worker_analysis_frame_stores_last_analysis() -> None:
    """analysis frame from worker with formatted text: stores in last_analysis."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("analysis", formatted="Screen analysis result"), is_worker=True)
    assert runtime.last_analysis == "Screen analysis result"


async def test_worker_analysis_frame_empty_formatted_ignored() -> None:
    """analysis frame from worker with empty formatted: last_analysis unchanged."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("analysis", formatted=""), is_worker=True)
    assert runtime.last_analysis is None
