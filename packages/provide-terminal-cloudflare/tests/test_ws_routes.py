#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
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
        self.browser_resume_tokens: dict[str, str] = {}
        self.browser_sockets: dict[str, object] = {}
        self._browser_role = browser_role
        self._sent: list[dict] = []
        self._pushed: list[str] = []
        self._broadcast: list[dict] = []
        self._snapshots_saved: list[dict] = []
        self._input_modes_saved: list[str] = []
        self._socket_roles: dict[str, str] = {}  # ws_key → role
        self.config = SimpleNamespace(limits=SimpleNamespace(max_ws_message_bytes=1_048_576, max_input_chars=10_000))
        self.store = SimpleNamespace(
            save_snapshot=lambda wid, snap: self._snapshots_saved.append(snap),
            save_input_mode=lambda wid, mode: self._input_modes_saved.append(mode),
        )
        self.ctx = SimpleNamespace(getWebSockets=list)
        self.worker_ws = None

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

    def _socket_role(self, ws: object) -> str:
        return self._socket_roles.get(self.ws_key(ws), "browser")


class _Ws:
    pass


def _raw(frame_type: str, **kwargs) -> str:
    return frame_json(frame_type, **kwargs)


# ---------------------------------------------------------------------------
# ProtocolError handling
# ---------------------------------------------------------------------------


async def test_protocol_error_sends_error_frame() -> None:
    """Malformed control channel → ProtocolError → error frame sent to ws."""
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


# ---------------------------------------------------------------------------
# Presence messages — _handle_presence_message
# ---------------------------------------------------------------------------


async def test_presence_dropped_when_not_enabled() -> None:
    """presence_update silently dropped when meta.presence is falsy."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("presence_update"), is_worker=False)
    assert not runtime._sent


async def test_presence_update_relayed_to_other_browsers() -> None:
    """presence_update relayed to all other browsers, not back to sender."""
    runtime = _Runtime()
    runtime.meta["presence"] = True
    sender = _Ws()
    other = _Ws()
    runtime.browser_sockets[runtime.ws_key(other)] = other
    runtime.ctx = SimpleNamespace(getWebSockets=lambda: [sender, other])
    runtime._socket_roles[runtime.ws_key(sender)] = "browser"
    runtime._socket_roles[runtime.ws_key(other)] = "browser"
    await handle_socket_message(runtime, sender, _raw("presence_update"), is_worker=False)
    assert len(runtime._sent) == 1


async def test_presence_update_skips_worker_sockets() -> None:
    """presence_update skips non-browser sockets."""
    runtime = _Runtime()
    runtime.meta["presence"] = True
    sender = _Ws()
    worker = _Ws()
    runtime.ctx = SimpleNamespace(getWebSockets=lambda: [sender, worker])
    runtime._socket_roles[runtime.ws_key(sender)] = "browser"
    runtime._socket_roles[runtime.ws_key(worker)] = "worker"
    await handle_socket_message(runtime, sender, _raw("presence_update"), is_worker=False)
    assert not runtime._sent


async def test_presence_update_send_failure_removes_socket() -> None:
    """Broadcast exception removes the failing socket from browser_sockets."""
    runtime = _Runtime()
    runtime.meta["presence"] = True
    sender = _Ws()
    broken = _Ws()
    broken_key = runtime.ws_key(broken)
    runtime.browser_sockets[broken_key] = broken
    runtime.ctx = SimpleNamespace(getWebSockets=lambda: [sender, broken])
    runtime._socket_roles[runtime.ws_key(sender)] = "browser"
    runtime._socket_roles[broken_key] = "browser"

    _orig_send = runtime.send_ws

    async def _failing_send(ws: object, frame: dict) -> None:
        if ws is broken:
            raise OSError("send failed")
        await _orig_send(ws, frame)

    runtime.send_ws = _failing_send  # type: ignore[assignment]
    await handle_socket_message(runtime, sender, _raw("presence_update"), is_worker=False)
    assert broken_key not in runtime.browser_sockets


async def test_presence_getwebsockets_failure_falls_back() -> None:
    """When ctx.getWebSockets() raises, falls back to browser_sockets."""
    runtime = _Runtime()
    runtime.meta["presence"] = True
    sender = _Ws()
    other = _Ws()
    runtime.browser_sockets[runtime.ws_key(other)] = other
    runtime.ctx = SimpleNamespace(getWebSockets=lambda: (_ for _ in ()).throw(RuntimeError))
    runtime._socket_roles[runtime.ws_key(sender)] = "browser"
    runtime._socket_roles[runtime.ws_key(other)] = "browser"
    await handle_socket_message(runtime, sender, _raw("presence_update"), is_worker=False)
    assert len(runtime._sent) == 1


async def test_presence_getwebsockets_empty_falls_back() -> None:
    """When ctx.getWebSockets() returns empty, falls back to browser_sockets."""
    runtime = _Runtime()
    runtime.meta["presence"] = True
    sender = _Ws()
    other = _Ws()
    runtime.browser_sockets[runtime.ws_key(other)] = other
    runtime.ctx = SimpleNamespace(getWebSockets=list)
    runtime._socket_roles[runtime.ws_key(other)] = "browser"
    await handle_socket_message(runtime, sender, _raw("presence_update"), is_worker=False)
    assert len(runtime._sent) == 1


async def test_control_request_relayed_to_hijack_owner() -> None:
    """control_request is relayed only to the current hijack owner."""
    runtime = _Runtime()
    runtime.meta["presence"] = True
    sender = _Ws()
    owner = _Ws()
    owner_key = runtime.ws_key(owner)
    runtime.browser_sockets[owner_key] = owner

    result = runtime.hijack.acquire("test", 60)
    assert result.session is not None
    runtime.browser_hijack_owner[owner_key] = result.session.hijack_id

    await handle_socket_message(runtime, sender, _raw("control_request"), is_worker=False)
    assert len(runtime._sent) == 1


async def test_control_request_no_owner_drops_silently() -> None:
    """control_request with no active hijack is silently dropped."""
    runtime = _Runtime()
    runtime.meta["presence"] = True
    sender = _Ws()
    await handle_socket_message(runtime, sender, _raw("control_request"), is_worker=False)
    assert not runtime._sent


async def test_control_request_sender_is_owner_drops() -> None:
    """control_request from the owner itself is not echoed back."""
    runtime = _Runtime()
    runtime.meta["presence"] = True
    sender = _Ws()
    sender_key = runtime.ws_key(sender)
    runtime.browser_sockets[sender_key] = sender

    result = runtime.hijack.acquire("test", 60)
    assert result.session is not None
    runtime.browser_hijack_owner[sender_key] = result.session.hijack_id

    await handle_socket_message(runtime, sender, _raw("control_request"), is_worker=False)
    assert not runtime._sent


async def test_control_request_owner_not_in_browser_sockets() -> None:
    """control_request with active hijack but owner not connected: dropped."""
    runtime = _Runtime()
    runtime.meta["presence"] = True
    sender = _Ws()
    bystander = _Ws()
    bystander_key = runtime.ws_key(bystander)
    runtime.browser_sockets[bystander_key] = bystander
    # Active hijack, but no browser has the matching hijack_id
    result = runtime.hijack.acquire("test", 60)
    assert result.session is not None
    runtime.browser_hijack_owner[bystander_key] = "wrong-hijack-id"
    await handle_socket_message(runtime, sender, _raw("control_request"), is_worker=False)
    assert not runtime._sent


async def test_control_request_send_failure_removes_owner() -> None:
    """control_request send failure removes owner socket from browser_sockets."""
    runtime = _Runtime()
    runtime.meta["presence"] = True
    sender = _Ws()
    owner = _Ws()
    owner_key = runtime.ws_key(owner)
    runtime.browser_sockets[owner_key] = owner

    result = runtime.hijack.acquire("test", 60)
    assert result.session is not None
    runtime.browser_hijack_owner[owner_key] = result.session.hijack_id

    async def _failing_send(ws: object, frame: dict) -> None:
        raise OSError("send failed")

    runtime.send_ws = _failing_send  # type: ignore[assignment]
    await handle_socket_message(runtime, sender, _raw("control_request"), is_worker=False)
    assert owner_key not in runtime.browser_sockets


# ---------------------------------------------------------------------------
# Intercept relay — http_action / http_intercept_toggle / http_inspect_toggle
# ---------------------------------------------------------------------------


async def test_http_action_relayed_to_worker() -> None:
    runtime = _Runtime(input_mode="open")
    runtime.worker_ws = _Ws()
    await handle_socket_message(runtime, _Ws(), _raw("http_action", id="r1", action="forward"), is_worker=False)
    assert len(runtime._sent) == 1
    assert runtime._sent[0]["type"] == "http_action"


async def test_http_intercept_toggle_relayed() -> None:
    runtime = _Runtime(input_mode="open")
    runtime.worker_ws = _Ws()
    await handle_socket_message(runtime, _Ws(), _raw("http_intercept_toggle", enabled=True), is_worker=False)
    assert runtime._sent[0]["type"] == "http_intercept_toggle"


async def test_http_inspect_toggle_relayed() -> None:
    runtime = _Runtime(input_mode="open")
    runtime.worker_ws = _Ws()
    await handle_socket_message(runtime, _Ws(), _raw("http_inspect_toggle", enabled=False), is_worker=False)
    assert runtime._sent[0]["type"] == "http_inspect_toggle"


async def test_http_action_dropped_no_worker() -> None:
    runtime = _Runtime(input_mode="open")
    runtime.worker_ws = None
    await handle_socket_message(runtime, _Ws(), _raw("http_action", id="r1", action="drop"), is_worker=False)
    assert not runtime._sent
