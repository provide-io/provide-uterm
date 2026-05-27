#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for api/ws_routes.py — handle_socket_message dispatch."""

from __future__ import annotations

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
    def __init__(self) -> None:
        self.closed: tuple[int, str] | None = None

    def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)


def _raw(frame_type: str, **kwargs) -> str:
    return frame_json(frame_type, **kwargs)


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
