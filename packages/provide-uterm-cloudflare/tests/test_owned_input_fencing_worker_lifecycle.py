#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Worker socket lifecycle fencing: replacement, generations, restore, and fail-closed paths."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cf_fencing_helpers import (
    _AttachmentWs,
    _BlockingWorkerWs,
    _BrowserWs,
    _control,
    _runtime,
)
from provide.uterm.cloudflare.contracts import frame_json
from provide.uterm.cloudflare.do.ushell import _recording_available


async def test_worker_replacement_reapplies_active_pause_and_displaces_old_generation() -> None:
    runtime = _runtime()
    old_worker = _BlockingWorkerWs()
    old_worker.release.set()
    assert await runtime.register_worker_socket(old_worker)
    old_generation = runtime._worker_generation
    acquired = runtime.hijack.acquire("lease-owner", 60)
    assert acquired.session is not None
    runtime.persist_lease(acquired.session)

    new_worker = _BlockingWorkerWs()
    new_worker.release.set()
    assert await runtime.register_worker_socket(new_worker)

    assert runtime.worker_ws is new_worker
    assert runtime._worker_generation != old_generation
    assert _control(new_worker.sent[0])["action"] == "pause"
    assert old_worker.closed == (1012, "worker replaced")
    row = runtime.store.load_session(runtime.worker_id)
    assert row is not None and row["worker_generation"] == runtime._worker_generation


async def test_displaced_worker_frames_are_rejected() -> None:
    runtime = _runtime()
    old_worker = _BlockingWorkerWs()
    old_worker.release.set()
    assert await runtime.register_worker_socket(old_worker)
    new_worker = _BlockingWorkerWs()
    new_worker.release.set()
    assert await runtime.register_worker_socket(new_worker)
    browser = _BrowserWs()
    runtime.browser_sockets[runtime.ws_key(browser)] = browser

    await runtime.webSocketMessage(old_worker, frame_json("term", data="stale-output"))

    assert browser.sent == []
    assert old_worker.closed == (1008, "stale worker generation")
    assert runtime.worker_ws is new_worker


async def test_current_worker_generation_accepts_a_rebound_edge_proxy() -> None:
    """CF may provide a new Python proxy for the same attached edge socket."""
    runtime = _runtime()
    original_proxy = _BlockingWorkerWs()
    original_proxy.release.set()
    assert await runtime.register_worker_socket(original_proxy)
    rebound_proxy = _BlockingWorkerWs()
    rebound_proxy.release.set()
    rebound_proxy.attachment = original_proxy.attachment

    assert await runtime.activate_worker_socket(rebound_proxy)
    assert runtime.worker_ws is rebound_proxy
    assert rebound_proxy.closed is None


async def test_same_generation_rebound_proxy_close_clears_worker_and_kv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close callbacks use the server generation, not Python proxy identity."""
    runtime = _runtime()
    original_proxy = _BlockingWorkerWs()
    original_proxy.release.set()
    assert await runtime.register_worker_socket(original_proxy)
    rebound_proxy = _BlockingWorkerWs()
    rebound_proxy.release.set()
    rebound_proxy.attachment = original_proxy.attachment
    assert await runtime.activate_worker_socket(rebound_proxy)
    kv_updates: list[dict[str, object]] = []

    async def record_kv(_env: object, _worker_id: str, **values: object) -> None:
        kv_updates.append(values)

    monkeypatch.setattr("provide.uterm.cloudflare.do.session_runtime.lifecycle.update_kv_session", record_kv)
    runtime.lifecycle_state = "running"

    await runtime.webSocketClose(original_proxy, 1000, "same edge socket closed")

    assert runtime.worker_ws is None
    assert runtime._worker_generation is None
    assert runtime.lifecycle_state == "stopped"
    assert kv_updates[-1]["connected"] is False


async def test_stale_worker_close_does_not_publish_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    old_worker = _BlockingWorkerWs()
    old_worker.release.set()
    assert await runtime.register_worker_socket(old_worker)
    new_worker = _BlockingWorkerWs()
    new_worker.release.set()
    assert await runtime.register_worker_socket(new_worker)
    broadcasts: list[dict[str, object]] = []

    async def record_broadcast(frame: dict[str, object]) -> None:
        broadcasts.append(frame)

    kv_updates: list[dict[str, object]] = []

    async def record_kv(_env: object, _worker_id: str, **values: object) -> None:
        kv_updates.append(values)

    runtime.broadcast_worker_frame = record_broadcast  # type: ignore[method-assign]
    monkeypatch.setattr("provide.uterm.cloudflare.do.session_runtime.lifecycle.update_kv_session", record_kv)
    runtime.lifecycle_state = "running"

    await runtime.webSocketClose(old_worker, 1000, "stale close")

    assert runtime.worker_ws is new_worker
    assert runtime.lifecycle_state == "running"
    assert broadcasts == []
    assert kv_updates == []


def test_new_persistence_reads_fail_closed() -> None:
    runtime = _runtime()
    assert runtime.store.load_runtime_activation("missing") is None
    runtime.store._exec(
        "INSERT INTO tunnel_invite_state(worker_id, entry_json, updated_at) VALUES(?, ?, ?)",
        ("broken", "{", 0.0),
    )
    assert runtime.store.load_tunnel_invite_state("broken") is None


def test_recording_availability_fails_closed_on_store_error() -> None:
    store = SimpleNamespace(current_event_seq=lambda _worker_id: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _recording_available(SimpleNamespace(store=store, worker_id="worker")) is False


def test_attachment_restore_rejects_non_mapping_and_foreign_resume_token() -> None:
    runtime = _runtime()
    assert runtime._attachment_data(_AttachmentWs('uterm-v2:["not", "a", "mapping"]')) == {}

    ws = _AttachmentWs("")
    runtime._serialize_socket_attachment(
        ws,
        role="browser",
        browser_role="admin",
        socket_id="foreign-browser",
        resume_token="foreign-token",
    )
    runtime._restore_browser_identity(ws)
    assert "foreign-browser" not in runtime.browser_resume_tokens


def test_worker_restore_preserves_explicit_display_name() -> None:
    runtime = _runtime()
    ws = _AttachmentWs("")
    runtime._serialize_socket_attachment(
        ws,
        role="worker",
        browser_role="admin",
        socket_id="worker-restored-worker",
        worker_generation="restored-generation",
    )
    runtime.worker_id = "default"
    runtime.meta["display_name"] = "explicit"
    runtime._restore_worker_id_from_socket(ws)
    assert runtime.worker_id == "fence-worker"
    assert runtime.meta["display_name"] == "explicit"


async def test_register_worker_idempotent_and_attachment_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    worker = _BlockingWorkerWs()
    worker.release.set()
    assert await runtime.register_worker_socket(worker)
    assert await runtime.register_worker_socket(worker)

    replacement = _BlockingWorkerWs()
    replacement.release.set()
    monkeypatch.setattr(
        runtime, "_serialize_socket_attachment", lambda *_args, **_kwargs: (_ for _ in ()).throw(TypeError())
    )
    assert await runtime.register_worker_socket(replacement)
    assert replacement._ut_role == "worker"
    assert replacement._ut_worker_generation == runtime._worker_generation


async def test_replacement_pause_failure_closes_candidate() -> None:
    runtime = _runtime()
    assert runtime.hijack.acquire("owner", 60).session is not None

    class FailingWorker(_BlockingWorkerWs):
        async def send(self, _data: str) -> None:
            raise RuntimeError("pause failed")

    worker = FailingWorker()
    assert not await runtime.register_worker_socket(worker)
    assert worker.closed == (1011, "failed to restore active lease")


async def test_ushell_control_and_input_fail_closed() -> None:
    runtime = _runtime()

    class FailingUshell:
        async def handle_control(self, _action: str) -> None:
            raise RuntimeError("control failed")

        async def handle_input(self, _data: str) -> list[dict[str, object]]:
            raise RuntimeError("input failed")

    runtime._ushell = FailingUshell()
    assert not await runtime.push_worker_control("pause", owner="owner", lease_s=1)
    assert not await runtime.push_worker_input("data")


async def test_failed_old_send_does_not_clear_replacement() -> None:
    runtime = _runtime()
    replacement = _BlockingWorkerWs()

    class ReplacingFailure(_BlockingWorkerWs):
        async def send(self, _data: str) -> None:
            runtime.worker_ws = replacement
            raise RuntimeError("old worker failed")

    runtime.worker_ws = ReplacingFailure()
    assert not await runtime._send_worker_frame({"type": "data", "data": "x"})
    assert runtime.worker_ws is replacement


async def test_lifecycle_rejects_worker_activation_and_removes_unknown_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    worker = _AttachmentWs("worker:admin:fence-worker")

    async def reject(_ws: object) -> bool:
        return False

    monkeypatch.setattr(runtime, "activate_worker_socket", reject)
    await runtime.webSocketOpen(worker)
    assert runtime.lifecycle_state == "stopped"

    unknown = _AttachmentWs("")
    unknown._ut_role = "unknown"

    def seed_unknown_socket() -> str:
        socket_id = runtime.ws_key(unknown)
        runtime.worker_ws = unknown
        runtime.browser_sockets[socket_id] = unknown
        runtime.raw_sockets[socket_id] = unknown
        runtime.browser_hijack_owner[socket_id] = "stale-hijack"
        runtime.browser_resume_tokens[socket_id] = "stale-token"
        return socket_id

    def assert_unknown_socket_removed(socket_id: str) -> None:
        assert runtime.worker_ws is None
        assert socket_id not in runtime.browser_sockets
        assert socket_id not in runtime.raw_sockets
        assert socket_id not in runtime.browser_hijack_owner
        assert socket_id not in runtime.browser_resume_tokens

    socket_id = seed_unknown_socket()
    await runtime.webSocketClose(unknown, 1000, "closed")
    assert_unknown_socket_removed(socket_id)

    socket_id = seed_unknown_socket()
    await runtime.webSocketError(unknown, RuntimeError("failed"))
    assert_unknown_socket_removed(socket_id)


async def test_worker_upgrade_rejects_failed_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    runtime.ctx.acceptWebSocket = lambda _ws: None
    runtime.register_worker_socket = AsyncMock(return_value=False)
    client = SimpleNamespace()
    server = _BlockingWorkerWs()
    pair = SimpleNamespace(object_values=lambda: (client, server))
    js = ModuleType("js")
    js.WebSocketPair = SimpleNamespace(new=lambda: pair)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "js", js)
    request = SimpleNamespace(
        url=f"https://example.invalid/ws/worker/{runtime.worker_id}/term",
        method="GET",
        headers={"Upgrade": "websocket", "Authorization": f"Bearer {runtime.config.worker_bearer_token}"},
    )

    response = await runtime._fetch_impl(request)

    assert response.status == 409
