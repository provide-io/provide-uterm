#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for api/ws_routes.py — handle_socket_message dispatch."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

from provide.uterm.cloudflare.api.ws_routes import handle_socket_message
from provide.uterm.cloudflare.bridge.hijack import HijackCoordinator
from provide.uterm.cloudflare.contracts import frame_json

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
        self._acks: list[tuple[str, int]] = []
        self._broadcast: list[dict] = []
        self._snapshots_saved: list[dict] = []
        self._input_modes_saved: list[str] = []
        self._runtime_incarnation = "test-incarnation"
        self._runtime_activation_seq = 7
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

    def input_delivery_guard(self):
        return nullcontext()

    async def push_worker_input(self, data: str) -> bool:
        self._pushed.append(data)
        return True

    async def note_browser_ack(self, ws_id: str, acked_bytes: int) -> None:
        self._acks.append((ws_id, acked_bytes))

    async def broadcast_worker_frame(self, frame: object) -> None:
        self._broadcast.append(frame)

    def ws_key(self, ws: object) -> str:
        return str(id(ws))

    def _socket_browser_role(self, ws: object) -> str:
        return self._browser_role

    def _socket_role(self, ws: object) -> str:
        return self._socket_roles.get(self.ws_key(ws), "browser")

    def _all_live_sockets(self) -> list:
        try:
            all_ws = list(self.ctx.getWebSockets())
        except Exception:
            all_ws = []
        if not all_ws:
            all_ws = list(self.browser_sockets.values())
        return all_ws


class _Ws:
    def __init__(self) -> None:
        self.closed: tuple[int, str] | None = None

    def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)


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


async def test_worker_hello_with_protocol_block_dict() -> None:
    """worker_hello with a ``protocol: {min, max}`` block: parsed and accepted."""
    runtime = _Runtime(input_mode="open")
    ws = _Ws()
    await handle_socket_message(
        runtime,
        ws,
        _raw("worker_hello", input_mode="hijack", protocol={"min": 1, "max": 1}),
        is_worker=True,
    )
    assert runtime.input_mode == "hijack"
    assert ws.closed is None  # negotiated successfully


async def test_worker_hello_with_legacy_protocol_version() -> None:
    """worker_hello with legacy ``protocol_version`` int: parsed and accepted."""
    runtime = _Runtime(input_mode="open")
    ws = _Ws()
    await handle_socket_message(
        runtime,
        ws,
        _raw("worker_hello", input_mode="hijack", protocol_version=1),
        is_worker=True,
    )
    assert runtime.input_mode == "hijack"
    assert ws.closed is None


async def test_worker_hello_protocol_mismatch_closes_socket() -> None:
    """worker_hello with a protocol range above the server's max: broadcasts error frame and closes."""
    runtime = _Runtime(input_mode="open")
    ws = _Ws()
    # Server MAX is 1; ask for [99, 99] so negotiation returns None.
    await handle_socket_message(
        runtime,
        ws,
        _raw("worker_hello", input_mode="hijack", protocol={"min": 99, "max": 99}),
        is_worker=True,
    )
    # The error frame must have been broadcast before the close.
    assert runtime._broadcast, "expected protocol_mismatch error frame"
    err = runtime._broadcast[-1]
    assert err["type"] == "error"
    assert err["reason"] == "protocol_mismatch"
    assert err["client_min"] == 99
    assert err["client_max"] == 99
    # The websocket must have been closed with the protocol-error code.
    assert ws.closed == (1002, "protocol_mismatch")
    # input_mode must not have been touched (we returned before applying it).
    assert runtime.input_mode == "open"


async def test_worker_hello_protocol_mismatch_survives_broadcast_failure() -> None:
    """Mismatch path: broadcast raising must not bubble — close still attempted."""
    runtime = _Runtime(input_mode="open")

    async def _boom(_frame: object) -> None:
        raise RuntimeError("broadcast failed")

    runtime.broadcast_worker_frame = _boom  # type: ignore[assignment]
    ws = _Ws()
    await handle_socket_message(
        runtime,
        ws,
        _raw("worker_hello", input_mode="hijack", protocol={"min": 99, "max": 99}),
        is_worker=True,
    )
    # Close should still have happened despite the broadcast failure.
    assert ws.closed == (1002, "protocol_mismatch")


async def test_worker_hello_protocol_mismatch_survives_close_failure() -> None:
    """Mismatch path: ws.close raising must not bubble out of handle_socket_message."""
    runtime = _Runtime(input_mode="open")
    ws = _Ws()

    def _boom(_code: int, _reason: str) -> None:
        raise RuntimeError("close failed")

    ws.close = _boom  # type: ignore[method-assign]
    # Should return cleanly even though close raised.
    await handle_socket_message(
        runtime,
        ws,
        _raw("worker_hello", input_mode="hijack", protocol={"min": 99, "max": 99}),
        is_worker=True,
    )
    # Broadcast still fired before the failing close.
    assert any(b.get("type") == "error" and b.get("reason") == "protocol_mismatch" for b in runtime._broadcast)


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
# Browser hijack controls report truthful refusal when no lease/worker exists
# ---------------------------------------------------------------------------


async def test_browser_hijack_request_without_worker_is_rejected() -> None:
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("hijack_request"), is_worker=False)
    assert runtime._sent[0]["message"] == "no_worker"


async def test_browser_hijack_release_without_ownership_is_rejected() -> None:
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("hijack_release"), is_worker=False)
    assert runtime._sent[0]["message"] == "not_owner"


async def test_browser_hijack_step_without_ownership_is_rejected() -> None:
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("hijack_step"), is_worker=False)
    assert runtime._sent[0]["message"] == "not_owner"


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


async def test_browser_ping_returns_runtime_witness() -> None:
    """Ping exposes the persisted cold-runtime activation witness."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("ping"), is_worker=False)
    assert runtime._sent == [
        {
            "type": "heartbeat",
            "runtime_incarnation": "test-incarnation",
            "runtime_activation_seq": 7,
            "ts": runtime._sent[0]["ts"],
        }
    ]


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


# ---------------------------------------------------------------------------
# ack frame → note_browser_ack (Tier-A backpressure ingestion)
# ---------------------------------------------------------------------------


async def test_ack_frame_with_int_bytes_forwarded() -> None:
    """A browser 'ack' frame forwards its cumulative byte count to note_browser_ack."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("ack", bytes=512), is_worker=False)
    assert runtime._acks == [(runtime.ws_key(ws), 512)]


async def test_ack_frame_with_non_int_bytes_defaults_zero() -> None:
    """A non-integer 'bytes' value is coerced to 0 (never raises)."""
    runtime = _Runtime()
    ws = _Ws()
    await handle_socket_message(runtime, ws, _raw("ack", bytes="oops"), is_worker=False)
    assert runtime._acks == [(runtime.ws_key(ws), 0)]
