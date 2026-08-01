#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Deterministic ownership-fencing races for the Cloudflare Durable Object."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from provide.uterm.cloudflare.api.http_routes import route_http
from provide.uterm.cloudflare.api.http_routes._hijack import route_hijack
from provide.uterm.cloudflare.api.ws_routes import handle_socket_message
from provide.uterm.cloudflare.contracts import frame_json
from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder

_KEY = "test-secret-key-32-bytes-minimum!"


class _Request:
    def __init__(
        self,
        body: dict[str, object] | None = None,
        *,
        url: str = "https://example.invalid/",
        method: str = "POST",
    ) -> None:
        self.headers = {"Content-Type": "application/json"}
        self._body = json.dumps(body or {})
        self.url = url
        self.method = method

    async def text(self) -> str:
        return self._body


class _BrowserWs:
    def __init__(self, role: str = "admin") -> None:
        self.role = role
        self.sent: list[str] = []

    def deserializeAttachment(self) -> str:  # noqa: N802 - Cloudflare WebSocket API
        return f"browser:{self.role}:fence-worker"

    def serializeAttachment(self, attachment: str) -> None:  # noqa: N802 - Cloudflare WebSocket API
        self.attachment = attachment

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _BlockingWorkerWs:
    """Hold the first worker send so a lifecycle transition can race it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.sent: list[str] = []
        self._blocked = False
        self.attachment = "worker:admin:fence-worker"
        self.closed: tuple[int, str] | None = None

    def deserializeAttachment(self) -> str:  # noqa: N802 - Cloudflare WebSocket API
        return self.attachment

    def serializeAttachment(self, attachment: str) -> None:  # noqa: N802 - Cloudflare WebSocket API
        self.attachment = attachment

    def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)

    async def send(self, data: str) -> None:
        self.sent.append(data)
        if not self._blocked:
            self._blocked = True
            self.started.set()
            await self.release.wait()


class _FailingBrowserWs(_BrowserWs):
    async def send(self, data: str) -> None:
        raise RuntimeError("browser disconnected")


def _runtime() -> SessionRuntime:
    conn = sqlite3.connect(":memory:")
    ctx = SimpleNamespace(
        storage=SimpleNamespace(sql=SimpleNamespace(exec=conn.execute), setAlarm=lambda _ms: None),
        id=SimpleNamespace(name=lambda: "fence-worker"),
        getWebSockets=list,
    )
    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM=_KEY,
        WORKER_BEARER_TOKEN="test-worker-token-padded-to-32xyz",
    )
    runtime = SessionRuntime(ctx, env)

    async def admin_role(_request: object) -> str:
        return "admin"

    runtime.browser_role_for_request = admin_role  # type: ignore[method-assign]
    return runtime


def _control(raw: str) -> dict[str, object]:
    chunks = ControlFrameDecoder().feed(raw)
    assert len(chunks) == 1 and isinstance(chunks[0], ControlChunk)
    return chunks[0].control


async def _release(runtime: SessionRuntime, hijack_id: str) -> object:
    return await route_hijack(
        runtime,
        _Request(),
        f"/worker/{runtime.worker_id}/hijack/{hijack_id}/release",
        "https://example.invalid/release",
        "POST",
    )


@pytest.mark.parametrize("source", ["rest_send", "rest_step", "browser"])
async def test_release_waits_for_owned_delivery(source: str) -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    hijack_id = acquired.session.hijack_id
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker

    if source == "rest_send":
        delivery = route_hijack(
            runtime,
            _Request({"keys": "owned-input"}),
            f"/worker/{runtime.worker_id}/hijack/{hijack_id}/send",
            "https://example.invalid/send",
            "POST",
        )
    elif source == "rest_step":
        delivery = route_hijack(
            runtime,
            _Request(),
            f"/worker/{runtime.worker_id}/hijack/{hijack_id}/step",
            "https://example.invalid/step",
            "POST",
        )
    else:
        browser = _BrowserWs()
        runtime.browser_hijack_owner[runtime.ws_key(browser)] = hijack_id
        delivery = handle_socket_message(runtime, browser, frame_json("input", data="owned-input"), is_worker=False)

    delivery_task = asyncio.create_task(delivery)
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    release_task = asyncio.create_task(_release(runtime, hijack_id))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not release_task.done(), "release crossed an in-flight owned delivery"
    assert runtime.hijack.session is not None

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    response = await asyncio.wait_for(release_task, timeout=1)
    assert getattr(response, "status", None) == 200
    assert runtime.hijack.session is None
    assert len(worker.sent) == 2


async def test_alarm_expiry_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    hijack_id = acquired.session.hijack_id
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker

    delivery_task = asyncio.create_task(
        route_hijack(
            runtime,
            _Request({"keys": "owned-input"}),
            f"/worker/{runtime.worker_id}/hijack/{hijack_id}/send",
            "https://example.invalid/send",
            "POST",
        )
    )
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    assert runtime.hijack._session is not None
    runtime.hijack._session.lease_expires_at = 0
    expired_raw_session = runtime.hijack._session
    assert runtime.hijack.session is None
    assert runtime.hijack._session is expired_raw_session

    alarm_task = asyncio.create_task(runtime.alarm())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not alarm_task.done(), "lease expiry crossed an in-flight owned delivery"

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    await asyncio.wait_for(alarm_task, timeout=1)
    assert runtime.hijack.session is None
    assert len(worker.sent) == 2


async def test_worker_replacement_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    old_worker = _BlockingWorkerWs()
    runtime.worker_ws = old_worker

    delivery_task = asyncio.create_task(
        route_hijack(
            runtime,
            _Request({"keys": "owned-input"}),
            f"/worker/{runtime.worker_id}/hijack/{acquired.session.hijack_id}/send",
            "https://example.invalid/send",
            "POST",
        )
    )
    await asyncio.wait_for(old_worker.started.wait(), timeout=1)
    new_worker = _BlockingWorkerWs()
    new_worker.release.set()
    replacement_task = asyncio.create_task(runtime.register_worker_socket(new_worker))
    await asyncio.sleep(0)

    assert not replacement_task.done()
    assert runtime.worker_ws is old_worker

    old_worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    await asyncio.wait_for(replacement_task, timeout=1)
    assert runtime.worker_ws is new_worker


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


async def test_worker_disconnect_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    worker = _BlockingWorkerWs()
    assert await runtime.register_worker_socket(worker)
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None

    delivery_task = asyncio.create_task(
        route_hijack(
            runtime,
            _Request({"keys": "owned-input"}),
            f"/worker/{runtime.worker_id}/hijack/{acquired.session.hijack_id}/send",
            "https://example.invalid/send",
            "POST",
        )
    )
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    disconnect_task = asyncio.create_task(runtime.unregister_worker_socket(worker))
    await asyncio.sleep(0)

    assert not disconnect_task.done()
    assert runtime.worker_ws is worker

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    assert await asyncio.wait_for(disconnect_task, timeout=1)
    assert runtime.worker_ws is None


async def test_worker_send_timeout_is_bounded_and_fails_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker
    monkeypatch.setattr(
        "provide.uterm.cloudflare.do.session_runtime.io._WORKER_SEND_TIMEOUT_S",
        0.01,
    )

    response = await asyncio.wait_for(
        route_hijack(
            runtime,
            _Request({"keys": "owned-input"}),
            f"/worker/{runtime.worker_id}/hijack/{acquired.session.hijack_id}/send",
            "https://example.invalid/send",
            "POST",
        ),
        timeout=0.5,
    )

    assert getattr(response, "status", None) == 409
    assert runtime.worker_ws is None


async def test_rest_heartbeat_uses_authenticated_acquirer_not_display_owner() -> None:
    runtime = _runtime()
    worker = _BlockingWorkerWs()
    worker.release.set()
    runtime.worker_ws = worker
    subject = "authenticated-subject"

    async def resolve(_request: object) -> tuple[object, None]:
        return SimpleNamespace(subject_id=subject), None

    runtime.resolve_principal = resolve  # type: ignore[method-assign]
    acquired_response = await route_hijack(
        runtime,
        _Request({"owner": "display-label", "lease_s": 30}),
        f"/worker/{runtime.worker_id}/hijack/acquire",
        "https://example.invalid/acquire",
        "POST",
    )
    assert getattr(acquired_response, "status", None) == 200
    active = runtime.hijack.session
    assert active is not None
    assert active.owner == "display-label"
    assert active.acquired_by == subject

    heartbeat = await route_hijack(
        runtime,
        _Request({"lease_s": 45}),
        f"/worker/{runtime.worker_id}/hijack/{active.hijack_id}/heartbeat",
        "https://example.invalid/heartbeat",
        "POST",
    )
    assert getattr(heartbeat, "status", None) == 200

    async def resolve_competitor(_request: object) -> tuple[object, None]:
        return SimpleNamespace(subject_id="different-subject"), None

    runtime.resolve_principal = resolve_competitor  # type: ignore[method-assign]
    refused = await route_hijack(
        runtime,
        _Request({"lease_s": 45}),
        f"/worker/{runtime.worker_id}/hijack/{active.hijack_id}/heartbeat",
        "https://example.invalid/heartbeat",
        "POST",
    )
    assert getattr(refused, "status", None) == 409


async def test_invalid_expect_regex_sends_zero_worker_frames() -> None:
    runtime = _runtime()
    active = runtime.hijack.acquire("owner", 60)
    assert active.session is not None
    worker = _BlockingWorkerWs()
    worker.release.set()
    runtime.worker_ws = worker

    response = await route_hijack(
        runtime,
        _Request({"keys": "must-not-send", "expect_regex": "["}),
        f"/worker/{runtime.worker_id}/hijack/{active.session.hijack_id}/send",
        "https://example.invalid/send",
        "POST",
    )

    assert getattr(response, "status", None) == 400
    assert worker.sent == []


@pytest.mark.parametrize(
    "unsafe_pattern",
    [
        "a*a*a*a*a*a*a*a*b",
        "a?a?a?a?a?a?a?a?b",
        r"(a)\1",
        r"(?P<letter>a)(?P=letter)",
    ],
)
async def test_unsafe_expect_regex_sends_zero_worker_frames(unsafe_pattern: str) -> None:
    runtime = _runtime()
    active = runtime.hijack.acquire("owner", 60)
    assert active.session is not None
    worker = _BlockingWorkerWs()
    worker.release.set()
    runtime.worker_ws = worker

    response = await route_hijack(
        runtime,
        _Request({"keys": "must-not-send", "expect_regex": unsafe_pattern}),
        f"/worker/{runtime.worker_id}/hijack/{active.session.hijack_id}/send",
        "https://example.invalid/send",
        "POST",
    )

    assert getattr(response, "status", None) == 400
    assert worker.sent == []


async def test_browser_hijack_control_is_public_and_owner_fenced() -> None:
    runtime = _runtime()
    worker = _BlockingWorkerWs()
    worker.release.set()
    runtime.worker_ws = worker
    owner = _BrowserWs()
    competitor = _BrowserWs()

    await handle_socket_message(runtime, owner, frame_json("hijack_request"), is_worker=False)
    active = runtime.hijack.session
    assert active is not None
    assert runtime.browser_hijack_owner[runtime.ws_key(owner)] == active.hijack_id
    assert _control(worker.sent[0])["action"] == "pause"

    before = len(worker.sent)
    await handle_socket_message(runtime, competitor, frame_json("hijack_step"), is_worker=False)
    assert len(worker.sent) == before
    assert any(_control(raw).get("message") == "not_owner" for raw in competitor.sent)

    await handle_socket_message(runtime, owner, frame_json("hijack_step"), is_worker=False)
    assert _control(worker.sent[-1])["action"] == "step"

    await handle_socket_message(runtime, owner, frame_json("hijack_release"), is_worker=False)
    assert runtime.hijack.session is None
    assert _control(worker.sent[-1])["action"] == "resume"


async def test_browser_owner_disconnect_can_resume_before_competitor() -> None:
    runtime = _runtime()
    worker = _BlockingWorkerWs()
    worker.release.set()
    runtime.worker_ws = worker
    owner = _BrowserWs()
    runtime.browser_sockets[runtime.ws_key(owner)] = owner
    runtime.store.create_resume_token("owner-token", runtime.worker_id, "admin", 60)
    runtime.browser_resume_tokens[runtime.ws_key(owner)] = "owner-token"
    await handle_socket_message(runtime, owner, frame_json("hijack_request"), is_worker=False)

    await runtime.webSocketClose(owner, 1000, "gone")
    assert runtime.hijack.session is None
    record = runtime.store.get_resume_token("owner-token")
    assert record is not None and record["was_hijack_owner"] is True

    resumed = _BrowserWs()
    await handle_socket_message(runtime, resumed, frame_json("resume", token="owner-token"), is_worker=False)

    assert runtime.hijack.session is not None
    assert runtime.browser_hijack_owner[runtime.ws_key(resumed)] == runtime.hijack.session.hijack_id
    assert any(_control(raw).get("resumed") is True for raw in resumed.sent)


async def test_stale_resume_cannot_steal_from_competing_browser() -> None:
    runtime = _runtime()
    worker = _BlockingWorkerWs()
    worker.release.set()
    runtime.worker_ws = worker
    owner = _BrowserWs()
    runtime.browser_sockets[runtime.ws_key(owner)] = owner
    runtime.store.create_resume_token("stale-token", runtime.worker_id, "admin", 60)
    runtime.browser_resume_tokens[runtime.ws_key(owner)] = "stale-token"
    await handle_socket_message(runtime, owner, frame_json("hijack_request"), is_worker=False)
    await runtime.webSocketClose(owner, 1000, "gone")

    competitor = _BrowserWs()
    runtime.store.create_resume_token("competitor-token", runtime.worker_id, "admin", 60)
    runtime.browser_resume_tokens[runtime.ws_key(competitor)] = "competitor-token"
    await handle_socket_message(runtime, competitor, frame_json("hijack_request"), is_worker=False)
    competing_session = runtime.hijack.session
    assert competing_session is not None

    stale = _BrowserWs()
    await handle_socket_message(runtime, stale, frame_json("resume", token="stale-token"), is_worker=False)

    assert runtime.hijack.session is competing_session
    assert runtime.browser_hijack_owner[runtime.ws_key(competitor)] == competing_session.hijack_id
    assert not any(_control(raw).get("resumed") is True for raw in stale.sent)


async def test_failed_browser_broadcast_removal_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    owner = _FailingBrowserWs()
    owner_key = runtime.ws_key(owner)
    runtime.browser_sockets[owner_key] = owner
    runtime.store.create_resume_token("failed-browser-token", runtime.worker_id, "admin", 60)
    runtime.browser_resume_tokens[owner_key] = "failed-browser-token"
    acquired = runtime.hijack.acquire("browser:failed-browser-token", 60)
    assert acquired.session is not None
    runtime.browser_hijack_owner[owner_key] = acquired.session.hijack_id
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker

    delivery_task = asyncio.create_task(
        route_hijack(
            runtime,
            _Request({"keys": "owned-input"}),
            f"/worker/{runtime.worker_id}/hijack/{acquired.session.hijack_id}/send",
            "https://example.invalid/send",
            "POST",
        )
    )
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    failed_broadcast = asyncio.create_task(runtime.broadcast_hijack_state())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not failed_broadcast.done()
    assert runtime.hijack.session is not None

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    await asyncio.wait_for(failed_broadcast, timeout=1)
    assert runtime.hijack.session is None
    record = runtime.store.get_resume_token("failed-browser-token")
    assert record is not None and record["was_hijack_owner"] is True


async def test_session_delete_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker

    delivery_task = asyncio.create_task(
        route_hijack(
            runtime,
            _Request({"keys": "owned-input"}),
            f"/worker/{runtime.worker_id}/hijack/{acquired.session.hijack_id}/send",
            "https://example.invalid/send",
            "POST",
        )
    )
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    delete_task = asyncio.create_task(
        route_http(
            runtime,
            _Request(
                url=f"https://example.invalid/api/sessions/{runtime.worker_id}",
                method="DELETE",
            ),
        )
    )
    await asyncio.sleep(0)

    assert not delete_task.done()
    assert runtime._deleted_at is None

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    response = await asyncio.wait_for(delete_task, timeout=1)
    assert getattr(response, "status", None) == 200
    assert runtime.lifecycle_state == "deleted"
    assert runtime.worker_ws is None
    assert runtime.hijack.session is None
